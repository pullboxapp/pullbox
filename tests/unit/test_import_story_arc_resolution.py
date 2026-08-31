"""Staged story-arc entries reconcile only through trusted local evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    ImportedStoryArcStatus,
    IssueStoryArc,
    StoryArc,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.services.import_story_arc_resolution import resolve_staged_story_arc_entries

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _job(session: AsyncSession) -> ImportJob:
    job = ImportJob(
        source_path="/private/source",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.FILE_MATCHING,
    )
    session.add(job)
    await session.flush()
    return job


async def _issue(
    session: AsyncSession,
    *,
    comicvine_id: int,
    title: str,
    issue_number: float,
    issue_number_text: str,
) -> Issue:
    series = Series(title=title, sort_title=title)
    session.add(series)
    await session.flush()
    issue = Issue(
        comicvine_id=comicvine_id,
        series_id=series.id,
        issue_number=issue_number,
        issue_number_text=issue_number_text,
    )
    session.add(issue)
    await session.flush()
    return issue


async def _arc(session: AsyncSession, job: ImportJob, *, ordinal: int = 1) -> ImportedStoryArc:
    arc = ImportedStoryArc(
        import_job_id=job.id,
        source_kind=StoryArcSourceKind.MYLAR3,
        source_key=f"mylar3:arc-{ordinal}",
        source_ordinal=ordinal,
        name=f"Arc {ordinal}",
        status=ImportedStoryArcStatus.DETECTED,
    )
    session.add(arc)
    await session.flush()
    return arc


async def _entry(
    session: AsyncSession,
    arc: ImportedStoryArc,
    *,
    ordinal: int,
    source_issue_id: str | None,
    issue_number: str,
    source_location: str | None = None,
    import_file_id: int | None = None,
    state: StoryArcResolutionState = StoryArcResolutionState.PENDING,
) -> ImportedStoryArcEntry:
    entry = ImportedStoryArcEntry(
        imported_story_arc_id=arc.id,
        import_file_id=import_file_id,
        source_ordinal=ordinal,
        reading_order=ordinal,
        reading_order_raw=f"{ordinal:03d}",
        resolution_state=state,
        source_kind=arc.source_kind,
        source_issue_id=source_issue_id,
        source_issue_number_text=issue_number,
        source_series_name="Exact Series",
        source_location=source_location,
    )
    session.add(entry)
    await session.flush()
    return entry


async def _file(
    session: AsyncSession,
    job: ImportJob,
    *,
    name: str,
    comicvine_issue_id: int | None,
    matched_issue_id: int | None,
    matched_issue_cv_id: int | None = None,
    status: ImportedFileStatus = ImportedFileStatus.MATCHED,
) -> ImportedFile:
    series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Exact Series",
        file_count=1,
    )
    session.add(series)
    await session.flush()
    item = ImportedFile(
        import_job_id=job.id,
        import_series_id=series.id,
        file_path=f"/private/source/{name}",
        file_name=name,
        file_size=100,
        file_format="cbz",
        comicvine_issue_id=comicvine_issue_id,
        matched_issue_id=matched_issue_id,
        matched_issue_cv_id=matched_issue_cv_id,
        status=status,
    )
    session.add(item)
    await session.flush()
    return item


@pytest.mark.asyncio
async def test_resolves_folder_link_and_mylar_provider_id_without_fuzzy_matching(
    db_session: AsyncSession,
) -> None:
    job = await _job(db_session)
    million = await _issue(
        db_session,
        comicvine_id=340001,
        title="Million",
        issue_number=1_000_000.0,
        issue_number_text="1000000",
    )
    annual = await _issue(
        db_session,
        comicvine_id=340002,
        title="Annual",
        issue_number=1.0,
        issue_number_text="1AU",
    )
    exact_file = await _file(
        db_session,
        job,
        name="Million 1000000.cbz",
        comicvine_issue_id=340001,
        matched_issue_id=million.id,
    )
    linked_file = await _file(
        db_session,
        job,
        name="Annual 1AU.cbz",
        comicvine_issue_id=340002,
        matched_issue_id=annual.id,
    )
    arc = await _arc(db_session, job)
    mylar = await _entry(
        db_session,
        arc,
        ordinal=1,
        source_issue_id="340001",
        issue_number="1000000",
    )
    folder = await _entry(
        db_session,
        arc,
        ordinal=2,
        source_issue_id=None,
        issue_number="1AU",
        import_file_id=linked_file.id,
    )
    fuzzy_only = await _entry(
        db_session,
        arc,
        ordinal=3,
        source_issue_id="not-a-provider-id",
        issue_number="1000000",
    )

    result = await resolve_staged_story_arc_entries(db_session, import_job_id=job.id)

    assert result.entries_examined == 3
    assert result.resolved == 2
    assert result.pending == 1
    assert mylar.import_file_id == exact_file.id
    assert mylar.matched_issue_id == million.id
    assert mylar.resolution_state == StoryArcResolutionState.RESOLVED
    assert mylar.resolution_method == "exact_source_issue_id"
    assert mylar.source_issue_number_text == "1000000"
    assert folder.matched_issue_id == annual.id
    assert folder.resolution_state == StoryArcResolutionState.RESOLVED
    assert folder.resolution_method == "linked_import_file"
    assert fuzzy_only.matched_issue_id is None
    assert fuzzy_only.resolution_state == StoryArcResolutionState.PENDING
    assert await db_session.scalar(select(func.count()).select_from(StoryArc)) == 0
    assert await db_session.scalar(select(func.count()).select_from(IssueStoryArc)) == 0


@pytest.mark.asyncio
async def test_exact_identity_conflict_and_safety_findings_fail_closed(
    db_session: AsyncSession,
) -> None:
    job = await _job(db_session)
    first = await _issue(
        db_session,
        comicvine_id=111,
        title="First",
        issue_number=1.0,
        issue_number_text="1",
    )
    conflict_file = await _file(
        db_session,
        job,
        name="Conflict.cbz",
        comicvine_issue_id=111,
        matched_issue_id=first.id,
    )
    blocked_file = await _file(
        db_session,
        job,
        name="Blocked.cbz",
        comicvine_issue_id=None,
        matched_issue_id=None,
        status=ImportedFileStatus.SAFETY_BLOCKED,
    )
    arc = await _arc(db_session, job)
    conflict = await _entry(
        db_session,
        arc,
        ordinal=1,
        source_issue_id="222",
        issue_number="1",
        import_file_id=conflict_file.id,
    )
    blocked = await _entry(
        db_session,
        arc,
        ordinal=2,
        source_issue_id=None,
        issue_number="2",
        import_file_id=blocked_file.id,
    )

    result = await resolve_staged_story_arc_entries(db_session, import_job_id=job.id)

    assert result.conflicts == 1
    assert result.ambiguous == 1
    assert conflict.resolution_state == StoryArcResolutionState.CONFLICT
    assert conflict.matched_issue_id is None
    assert conflict.diagnostics["review_reason"] == "conflicting_exact_issue_identity"
    assert blocked.resolution_state == StoryArcResolutionState.AMBIGUOUS
    assert blocked.diagnostics["review_reason"] == "source_file_safety_blocked"


@pytest.mark.asyncio
async def test_missing_entry_can_resolve_later_but_skipped_entry_stays_skipped(
    db_session: AsyncSession,
) -> None:
    job = await _job(db_session)
    issue = await _issue(
        db_session,
        comicvine_id=9001,
        title="Later",
        issue_number=0.5,
        issue_number_text="0.5",
    )
    arc = await _arc(db_session, job)
    missing = await _entry(
        db_session,
        arc,
        ordinal=1,
        source_issue_id="9001",
        issue_number="0.5",
        state=StoryArcResolutionState.MISSING,
    )
    skipped = await _entry(
        db_session,
        arc,
        ordinal=2,
        source_issue_id="9001",
        issue_number="0.5",
        state=StoryArcResolutionState.SKIPPED,
    )

    result = await resolve_staged_story_arc_entries(db_session, import_job_id=job.id)

    assert result.resolved == 1
    assert result.skipped == 1
    assert missing.matched_issue_id == issue.id
    assert missing.resolution_state == StoryArcResolutionState.RESOLVED
    assert skipped.matched_issue_id is None
    assert skipped.resolution_state == StoryArcResolutionState.SKIPPED


@pytest.mark.asyncio
async def test_resolution_checks_cancellation_between_bounded_batches(
    db_session: AsyncSession,
) -> None:
    job = await _job(db_session)
    arc = await _arc(db_session, job)
    for ordinal in range(1, 4):
        await _entry(
            db_session,
            arc,
            ordinal=ordinal,
            source_issue_id=None,
            issue_number=str(ordinal),
        )

    checkpoints = 0

    async def cancel_before_second_batch() -> None:
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints == 3:
            raise RuntimeError("synthetic cancellation")

    with pytest.raises(RuntimeError, match="synthetic cancellation"):
        await resolve_staged_story_arc_entries(
            db_session,
            import_job_id=job.id,
            batch_size=1,
            cancellation_check=cancel_before_second_batch,
        )

    assert checkpoints == 3
