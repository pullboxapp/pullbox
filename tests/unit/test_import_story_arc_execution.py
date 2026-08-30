"""Step 4 integrates logical story arcs after canonical file processing."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

import pullbox.services.import_job_execution as execution_module
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobAction,
    ImportJobStatus,
    ImportSeriesStatus,
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
from pullbox.services.import_job_actions import record_action
from pullbox.services.import_job_execution import execute_import_job

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class _UnusedSeriesService:
    """Arc-only jobs never invoke a metadata provider service."""


async def _execute(
    session: AsyncSession,
    job: ImportJob,
    *,
    process_series_files: object | None = None,
    log_event: AsyncMock | None = None,
) -> AsyncMock:
    logs = log_event or AsyncMock()
    await execute_import_job(
        session,
        job.id,
        series_service=_UnusedSeriesService(),  # type: ignore[arg-type]
        process_series_files=(
            process_series_files  # type: ignore[arg-type]
            if process_series_files is not None
            else AsyncMock()
        ),
        raise_if_cancelled=AsyncMock(),
        record_action=record_action,
        log_event=logs,
        emit_progress=AsyncMock(),
        estimate_remaining_seconds=lambda *_args: None,
        maybe_slow_item_delay=AsyncMock(),
        progress_callback=None,
    )
    return logs


@pytest.mark.asyncio
async def test_arc_only_execution_preserves_million_issue_and_missing_order(
    db_session: AsyncSession,
) -> None:
    job = ImportJob(
        source_path="/private/mylar/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.REVIEW,
    )
    db_session.add(job)
    await db_session.flush()
    staged = ImportedStoryArc(
        import_job_id=job.id,
        source_kind=StoryArcSourceKind.MYLAR3,
        source_key="mylar3:arc-only",
        source_arc_id="arc-only",
        source_ordinal=1,
        name="Arc Only",
        status=ImportedStoryArcStatus.CONFIRMED,
        selected_for_import=True,
    )
    db_session.add(staged)
    await db_session.flush()
    entry = ImportedStoryArcEntry(
        imported_story_arc_id=staged.id,
        source_ordinal=17,
        reading_order=None,
        reading_order_raw=None,
        source_kind=StoryArcSourceKind.MYLAR3,
        source_entry_id="entry-million",
        source_arc_id=staged.source_arc_id,
        source_issue_number_text="1000000",
        resolution_state=StoryArcResolutionState.MISSING,
        selected_for_import=True,
    )
    db_session.add(entry)
    await db_session.flush()

    logs = await _execute(db_session, job)

    membership = (await db_session.scalars(select(IssueStoryArc))).one()
    actions = list(
        (
            await db_session.scalars(select(ImportJobAction).order_by(ImportJobAction.sequence_no))
        ).all()
    )
    assert job.status == ImportJobStatus.COMPLETED
    assert staged.status == ImportedStoryArcStatus.IMPORTED
    assert membership.issue_id is None
    assert membership.source_issue_number_text == "1000000"
    assert membership.sequence_number == 17
    assert membership.legacy_sequence_was_null is True
    assert membership.resolution_state == StoryArcResolutionState.MISSING
    assert [action.action_type for action in actions] == [
        "story_arc_created",
        "story_arc_external_identity_created",
        "story_arc_membership_created",
    ]
    completion = next(
        call
        for call in logs.await_args_list
        if len(call.args) >= 4 and call.args[3] == "story_arc_materialization_completed"
    )
    assert completion.kwargs["arcs_examined"] == 1
    assert completion.kwargs["memberships_created"] == 1
    assert "source_path" not in completion.kwargs


@pytest.mark.asyncio
async def test_post_file_resolution_links_new_canonical_issue(
    db_session: AsyncSession,
) -> None:
    job = ImportJob(
        source_path="/private/source",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
    )
    series = Series(title="Late Resolution", sort_title="late resolution")
    db_session.add_all([job, series])
    await db_session.flush()
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Late Resolution",
        status=ImportSeriesStatus.DUPLICATE,
        series_id=series.id,
        file_count=1,
    )
    db_session.add(imported_series)
    await db_session.flush()
    imported_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=imported_series.id,
        file_path="/private/source/Late Resolution 1000000.cbz",
        file_name="Late Resolution 1000000.cbz",
        file_size=100,
        file_format="cbz",
        status=ImportedFileStatus.MATCHED,
        include_in_import=True,
    )
    db_session.add(imported_file)
    await db_session.flush()
    staged = ImportedStoryArc(
        import_job_id=job.id,
        source_kind=StoryArcSourceKind.FOLDER,
        source_key="folder:late-resolution",
        source_ordinal=1,
        name="Late Resolution Arc",
        status=ImportedStoryArcStatus.CONFIRMED,
        selected_for_import=True,
    )
    db_session.add(staged)
    await db_session.flush()
    entry = ImportedStoryArcEntry(
        imported_story_arc_id=staged.id,
        import_file_id=imported_file.id,
        source_ordinal=1,
        reading_order=1_000_000,
        reading_order_raw="1000000",
        source_kind=StoryArcSourceKind.FOLDER,
        source_entry_id="late-entry",
        source_issue_number_text="1000000",
        resolution_state=StoryArcResolutionState.PENDING,
        selected_for_import=True,
    )
    db_session.add(entry)
    await db_session.flush()
    created_issue_id: int | None = None

    async def process_files(
        session: AsyncSession,
        _job: ImportJob,
        _item: ImportedSeries,
        **_kwargs: object,
    ) -> tuple[int, int]:
        nonlocal created_issue_id
        issue = Issue(
            series_id=series.id,
            issue_number=1_000_000.0,
            issue_number_text="1000000",
        )
        session.add(issue)
        await session.flush()
        created_issue_id = issue.id
        persisted_file = await session.get(ImportedFile, imported_file.id)
        assert persisted_file is not None
        persisted_file.matched_issue_id = issue.id
        persisted_file.status = ImportedFileStatus.IMPORTED
        return 1, 0

    await _execute(db_session, job, process_series_files=process_files)

    membership = (await db_session.scalars(select(IssueStoryArc))).one()
    assert created_issue_id is not None
    assert entry.matched_issue_id == created_issue_id
    assert entry.resolution_state == StoryArcResolutionState.RESOLVED
    assert membership.issue_id == created_issue_id
    assert membership.sequence_number == 1_000_000
    assert membership.source_issue_number_text == "1000000"


@pytest.mark.asyncio
async def test_materialization_failure_keeps_committed_canonical_registration(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = ImportJob(
        source_path="/private/source/should-not-leak",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
    )
    series = Series(title="Canonical Survives", sort_title="canonical survives")
    db_session.add_all([job, series])
    await db_session.flush()
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Canonical Survives",
        status=ImportSeriesStatus.DUPLICATE,
        series_id=series.id,
        file_count=1,
    )
    db_session.add(imported_series)
    await db_session.flush()
    imported_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=imported_series.id,
        file_path="/private/source/should-not-leak/Issue.cbz",
        file_name="Issue.cbz",
        file_size=100,
        file_format="cbz",
        status=ImportedFileStatus.MATCHED,
        include_in_import=True,
    )
    staged = ImportedStoryArc(
        import_job_id=job.id,
        source_kind=StoryArcSourceKind.FOLDER,
        source_key="folder:failing",
        source_ordinal=1,
        name="Failing Arc",
        status=ImportedStoryArcStatus.CONFIRMED,
        selected_for_import=True,
    )
    db_session.add_all([imported_file, staged])
    await db_session.flush()

    async def process_files(
        session: AsyncSession,
        _job: ImportJob,
        _item: ImportedSeries,
        **_kwargs: object,
    ) -> tuple[int, int]:
        issue = Issue(series_id=series.id, issue_number=1.0, issue_number_text="1")
        session.add(issue)
        await session.flush()
        imported_file.matched_issue_id = issue.id
        imported_file.status = ImportedFileStatus.IMPORTED
        return 1, 0

    async def fail_materialization(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("/private/source/should-not-leak")

    monkeypatch.setattr(
        execution_module,
        "materialize_confirmed_story_arcs",
        fail_materialization,
        raising=False,
    )
    logs = await _execute(db_session, job, process_series_files=process_files)

    await db_session.refresh(job)
    await db_session.refresh(staged)
    assert job.status == ImportJobStatus.COMPLETED
    assert job.total_files_imported == 1
    assert job.error_message == "Story-arc registration failed; canonical files remain imported."
    assert staged.status == ImportedStoryArcStatus.FAILED
    assert await db_session.scalar(select(func.count()).select_from(Issue)) == 1
    assert await db_session.scalar(select(func.count()).select_from(StoryArc)) == 0
    failure = next(
        call
        for call in logs.await_args_list
        if len(call.args) >= 4 and call.args[3] == "story_arc_materialization_failed"
    )
    assert failure.kwargs["failure_type"] == "RuntimeError"
    assert "/private" not in failure.kwargs["message"]
