"""Truthful completion rules for import-owned Story Arc placements."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm.attributes import flag_modified

from pullbox.core.exceptions import ValidationError
from pullbox.models import Base
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportJob,
    ImportJobAction,
    ImportJobActionStatus,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    ImportedStoryArcStatus,
    IssueStoryArc,
    StoryArc,
    StoryArcPlacement,
    StoryArcPlacementMode,
    StoryArcPlacementOwnership,
    StoryArcPlacementState,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.models.story_arc_sync import StoryArcSyncWork, StoryArcSyncWorkState
from pullbox.services.import_job_controls import pause_job, request_cancel
from pullbox.services.import_story_arc_placement_completion import (
    ImportStoryArcPlacementCompletionState,
    _origin_work_page_statement,
    finalize_import_story_arc_placements,
    inspect_import_story_arc_placement_origin,
    seal_import_story_arc_placement_origin,
)
from pullbox.tasks.import_task import ImportRunner

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Dialect
    from sqlalchemy.ext.asyncio import AsyncSession


_COMPLETION_TIME = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()])
def test_origin_page_never_casts_untrusted_json_in_sql(dialect: Dialect) -> None:
    statement = _origin_work_page_statement(7, after_action_id=0, limit=1_000)

    compiled = str(statement.compile(dialect=dialect))

    assert "json_each" not in compiled.lower()
    assert "json_extract" not in compiled.lower()
    assert " AS INTEGER" not in compiled.upper()


async def _seed_job(
    session: AsyncSession,
    states: list[StoryArcSyncWorkState],
    *,
    status: ImportJobStatus = ImportJobStatus.IMPORTING,
) -> tuple[ImportJob, list[ImportJobAction], list[StoryArcSyncWork]]:
    job = ImportJob(
        source_path="/private/imports/customer-secret/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=status,
        progress_snapshot={
            "status": status.value,
            "phase": "story_arc_placements",
            "progress": 99,
            "story_arc_placements_total": len(states),
        },
        progress_revision=7,
    )
    session.add(job)
    await session.flush()
    root = LibraryRoot(name=f"Import root {job.id}", path=f"/library/{job.id}", enabled=True)
    series = Series(title=f"Series {job.id}", sort_title=f"series {job.id}", library_root=root)
    session.add(series)
    await session.flush()

    actions: list[ImportJobAction] = []
    works: list[StoryArcSyncWork] = []
    for ordinal, state in enumerate(states, start=1):
        generation = f"{ordinal:064x}"
        issue = Issue(
            series=series,
            issue_number=ordinal,
            issue_number_text=str(ordinal),
        )
        library_file = LibraryFile(
            file_path=f"/private/library/customer-secret/issue-{ordinal}.cbz",
            file_name=f"issue-{ordinal}.cbz",
            file_size=ordinal,
            file_format=FileFormat.CBZ,
            file_modified_at=_COMPLETION_TIME,
            match_confidence=MatchConfidence.HIGH,
            issue=issue,
            library_root=root,
        )
        story_arc = StoryArc(
            name=f"Imported arc {ordinal}",
            source_kind=StoryArcSourceKind.MYLAR3,
        )
        membership = IssueStoryArc(
            story_arc=story_arc,
            issue=issue,
            sequence_number=ordinal,
            source_ordinal=ordinal,
            resolution_state=StoryArcResolutionState.RESOLVED,
            source_kind=StoryArcSourceKind.MYLAR3,
        )
        staged_arc = ImportedStoryArc(
            import_job=job,
            source_kind=StoryArcSourceKind.MYLAR3,
            source_key=f"mylar3:arc:{ordinal}",
            source_ordinal=ordinal,
            name=story_arc.name,
            status=ImportedStoryArcStatus.IMPORTED,
            materialized_story_arc=story_arc,
        )
        staged_entry = ImportedStoryArcEntry(
            imported_story_arc=staged_arc,
            matched_issue=issue,
            materialized_membership=membership,
            source_ordinal=ordinal,
            reading_order=ordinal,
            resolution_state=StoryArcResolutionState.RESOLVED,
            source_kind=StoryArcSourceKind.MYLAR3,
        )
        session.add_all([library_file, membership, staged_entry])
        await session.flush()
        action = ImportJobAction(
            import_job_id=job.id,
            sequence_no=ordinal,
            phase="story_arc_placements",
            action_type="story_arc_managed_placement_requested",
            status=ImportJobActionStatus.COMPLETED,
            payload={},
        )
        session.add(action)
        await session.flush()
        work = StoryArcSyncWork(
            issue_story_arc_id=membership.id,
            library_file_id=library_file.id,
            origin_import_action_id=action.id,
            origin_import_job_id=job.id,
            origin_imported_story_arc_id=staged_arc.id,
            origin_imported_story_arc_entry_id=staged_entry.id,
            desired_generation=generation,
            source_signature_hash=f"{ordinal + 100:064x}",
            source_file_path=library_file.file_path,
            source_file_size=ordinal,
            source_file_modified_at=_COMPLETION_TIME,
            story_arc_revision=1,
            membership_sequence=ordinal,
            policy_schema_version=1,
            state=state,
            last_error_detail="private provider response and /private/path",
        )
        session.add(work)
        await session.flush()
        action.payload = {
            "schema_version": 1,
            "sync_work_id": work.id,
            "membership_id": work.issue_story_arc_id,
            "desired_generation": generation,
            "imported_story_arc_id": staged_arc.id,
            "imported_story_arc_entry_id": staged_entry.id,
            "source_import_job_id": job.id,
        }
        actions.append(action)
        works.append(work)
    await session.flush()
    return job, actions, works


def _add_current_placement(
    session: AsyncSession,
    *,
    job: ImportJob,
    action: ImportJobAction,
    work: StoryArcSyncWork,
    mode: StoryArcPlacementMode = StoryArcPlacementMode.COPY,
    ownership: StoryArcPlacementOwnership = StoryArcPlacementOwnership.MANAGED,
    state: StoryArcPlacementState = StoryArcPlacementState.CURRENT,
    source_import_job_id: int | None = None,
) -> StoryArcPlacement:
    placement = StoryArcPlacement(
        issue_story_arc_id=work.issue_story_arc_id,
        library_file_id=work.library_file_id,
        library_root_id=None,
        placement_path=f"/private/story-arcs/customer-secret/{action.id}.cbz",
        mode=mode,
        ownership=ownership,
        source_kind=StoryArcSourceKind.PULLBOX,
        source_import_job_id=(job.id if source_import_job_id is None else source_import_job_id),
        creating_action_id=action.id,
        rendered_reading_order=work.membership_sequence,
        policy_schema_version=work.policy_schema_version,
        source_fingerprint={"schema_version": 1, "size": work.source_file_size},
        state=state,
        last_result={"schema_version": 1, "status": "complete"},
        last_checked_at=_COMPLETION_TIME,
    )
    session.add(placement)
    return placement


@pytest.mark.asyncio
async def test_pending_work_keeps_importing_and_projects_all_six_sanitized_counts(
    db_session: AsyncSession,
) -> None:
    job, actions, works = await _seed_job(
        db_session,
        [
            StoryArcSyncWorkState.QUEUED,
            StoryArcSyncWorkState.RUNNING,
            StoryArcSyncWorkState.RETRY_WAIT,
            StoryArcSyncWorkState.COMPLETED,
        ],
        status=ImportJobStatus.STALLED,
    )
    _add_current_placement(
        db_session,
        job=job,
        action=actions[-1],
        work=works[-1],
    )
    await db_session.flush()

    with patch(
        "pullbox.services.import_story_arc_placement_completion."
        "_count_invalid_completed_placements",
        new_callable=AsyncMock,
    ) as count_invalid_placements:
        outcome = await finalize_import_story_arc_placements(db_session, job.id)

    count_invalid_placements.assert_not_awaited()

    assert outcome.state is ImportStoryArcPlacementCompletionState.PENDING
    assert outcome.counts.as_dict() == {
        "queued": 1,
        "running": 1,
        "retry_wait": 1,
        "failed": 0,
        "completed": 1,
        "cancelled": 0,
    }
    assert job.status is ImportJobStatus.IMPORTING
    assert job.import_completed_at is None
    assert job.error_message is None
    assert job.progress_snapshot["phase"] == "story_arc_placements"
    assert job.progress_snapshot["progress"] == 99
    for state, count in outcome.counts.as_dict().items():
        assert job.progress_snapshot[f"story_arc_placements_{state}"] == count
    rendered = f"{job.progress_snapshot!r} {job.error_message!r} {outcome!r}"
    assert "customer-secret" not in rendered
    assert "provider response" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_state", "expected_code"),
    [
        (StoryArcSyncWorkState.FAILED, "story_arc_placement_work_failed"),
        (StoryArcSyncWorkState.CANCELLED, "story_arc_placement_work_cancelled"),
    ],
)
async def test_terminal_work_stalls_with_an_actionable_sanitized_error(
    db_session: AsyncSession,
    terminal_state: StoryArcSyncWorkState,
    expected_code: str,
) -> None:
    job, _actions, _works = await _seed_job(db_session, [terminal_state])

    outcome = await finalize_import_story_arc_placements(db_session, job.id)

    assert outcome.state is ImportStoryArcPlacementCompletionState.STALLED
    assert outcome.error_code == expected_code
    assert job.status is ImportJobStatus.STALLED
    assert job.progress_snapshot["phase"] == "story_arc_placements"
    assert job.progress_snapshot["status"] == ImportJobStatus.STALLED.value
    assert job.import_completed_at is None
    assert job.error_message is not None
    assert "Retry" in job.error_message
    assert "/private/" not in job.error_message
    assert "provider response" not in job.error_message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    [
        "extra_payload_key",
        "generation",
        "missing_work",
        "staged_binding",
        "string_integer",
        "float_integer",
        "boolean_integer",
        "oversized_integer",
        "malformed_integer",
    ],
)
async def test_invalid_origin_binding_stalls_closed(
    db_session: AsyncSession,
    corruption: str,
) -> None:
    job, actions, works = await _seed_job(
        db_session,
        [StoryArcSyncWorkState.COMPLETED],
    )
    _add_current_placement(db_session, job=job, action=actions[0], work=works[0])
    if corruption == "extra_payload_key":
        actions[0].payload = {**actions[0].payload, "private_path": "/do/not/expose"}
    elif corruption == "generation":
        actions[0].payload = {**actions[0].payload, "desired_generation": "f" * 64}
    elif corruption == "missing_work":
        await db_session.delete(works[0])
    elif corruption == "staged_binding":
        staged_entry = await db_session.get(
            ImportedStoryArcEntry,
            int(actions[0].payload["imported_story_arc_entry_id"]),
        )
        assert staged_entry is not None
        staged_entry.materialized_membership_id = None
    elif corruption == "string_integer":
        actions[0].payload = {
            **actions[0].payload,
            "sync_work_id": str(works[0].id),
        }
    elif corruption == "float_integer":
        actions[0].payload = {
            **actions[0].payload,
            "sync_work_id": float(works[0].id),
        }
    elif corruption == "boolean_integer":
        actions[0].payload = {**actions[0].payload, "sync_work_id": True}
    elif corruption == "oversized_integer":
        actions[0].payload = {
            **actions[0].payload,
            "imported_story_arc_id": 2**80,
        }
    else:
        actions[0].payload = {
            **actions[0].payload,
            "imported_story_arc_entry_id": "not-an-integer",
        }
    flag_modified(actions[0], "payload")
    await db_session.flush()

    outcome = await finalize_import_story_arc_placements(db_session, job.id)

    assert outcome.state is ImportStoryArcPlacementCompletionState.STALLED
    assert outcome.error_code == "story_arc_placement_origin_invalid"
    assert job.status is ImportJobStatus.STALLED
    assert "retry" not in (job.error_message or "").lower()
    assert "roll back" in (job.error_message or "").lower()
    assert "do/not/expose" not in (job.error_message or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "placement_problem",
    ["missing", "referenced", "not_current", "wrong_job", "wrong_membership"],
)
async def test_completed_work_requires_one_exact_action_owned_current_managed_placement(
    db_session: AsyncSession,
    placement_problem: str,
) -> None:
    job, actions, works = await _seed_job(
        db_session,
        [StoryArcSyncWorkState.COMPLETED],
    )
    if placement_problem != "missing":
        placement = _add_current_placement(
            db_session,
            job=job,
            action=actions[0],
            work=works[0],
            mode=(
                StoryArcPlacementMode.REFERENCE_ONLY
                if placement_problem == "referenced"
                else StoryArcPlacementMode.COPY
            ),
            ownership=(
                StoryArcPlacementOwnership.REFERENCED
                if placement_problem == "referenced"
                else StoryArcPlacementOwnership.MANAGED
            ),
            state=(
                StoryArcPlacementState.MISSING
                if placement_problem == "not_current"
                else StoryArcPlacementState.CURRENT
            ),
        )
        if placement_problem == "wrong_job":
            other_job = ImportJob(
                source_path="/private/imports/other/mylar.db",
                source_type=ImportSourceType.MYLAR3,
                status=ImportJobStatus.COMPLETED,
            )
            db_session.add(other_job)
            await db_session.flush()
            placement.source_import_job_id = other_job.id
        if placement_problem == "wrong_membership":
            original_membership = await db_session.get(
                IssueStoryArc,
                works[0].issue_story_arc_id,
            )
            assert original_membership is not None
            unrelated_arc = StoryArc(
                name="Unrelated valid membership",
                source_kind=StoryArcSourceKind.PULLBOX,
            )
            unrelated_membership = IssueStoryArc(
                story_arc=unrelated_arc,
                issue_id=original_membership.issue_id,
                sequence_number=999,
                source_ordinal=999,
                resolution_state=StoryArcResolutionState.RESOLVED,
                source_kind=StoryArcSourceKind.PULLBOX,
            )
            db_session.add(unrelated_membership)
            await db_session.flush()
            placement.issue_story_arc_id = unrelated_membership.id
    await db_session.flush()

    outcome = await finalize_import_story_arc_placements(db_session, job.id)

    assert outcome.state is ImportStoryArcPlacementCompletionState.STALLED
    assert outcome.error_code == "story_arc_placement_evidence_invalid"
    assert job.status is ImportJobStatus.STALLED
    assert job.progress_snapshot["phase"] == "story_arc_placements"
    assert "retry" not in (job.error_message or "").lower()
    assert "roll back" in (job.error_message or "").lower()


@pytest.mark.asyncio
async def test_all_completed_exact_placements_complete_once_without_committing(
    db_session: AsyncSession,
) -> None:
    job, actions, works = await _seed_job(
        db_session,
        [StoryArcSyncWorkState.COMPLETED, StoryArcSyncWorkState.COMPLETED],
    )
    for action, work in zip(actions, works, strict=True):
        _add_current_placement(db_session, job=job, action=action, work=work)
    await db_session.flush()

    commit = AsyncMock()
    with patch.object(db_session, "commit", commit):
        first = await finalize_import_story_arc_placements(
            db_session,
            job.id,
            now=_COMPLETION_TIME,
        )
        completed_revision = job.progress_revision
        second = await finalize_import_story_arc_placements(
            db_session,
            job.id,
            now=_COMPLETION_TIME,
        )

    assert first.state is ImportStoryArcPlacementCompletionState.COMPLETED
    assert second == first
    assert job.status is ImportJobStatus.COMPLETED
    assert job.import_completed_at == _COMPLETION_TIME
    assert job.error_message is None
    assert job.progress_snapshot["status"] == ImportJobStatus.COMPLETED.value
    assert job.progress_snapshot["phase"] == "done"
    assert job.progress_snapshot["progress"] == 100
    assert job.progress_snapshot["story_arc_placements_completed"] == 2
    assert job.progress_snapshot["story_arc_placement_followup_pending"] is True
    assert job.story_arc_placement_followup_pending is True
    assert job.progress_revision == completed_revision
    commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_origin_inspection_counts_every_exact_action_across_all_work_states(
    db_session: AsyncSession,
) -> None:
    states = list(StoryArcSyncWorkState)
    job, _actions, _works = await _seed_job(db_session, states)

    counts = await inspect_import_story_arc_placement_origin(db_session, job.id)

    assert counts.as_dict() == {
        "queued": 1,
        "running": 1,
        "retry_wait": 1,
        "failed": 1,
        "completed": 1,
        "cancelled": 1,
    }
    assert counts.total == len(states)


@pytest.mark.asyncio
async def test_seal_counts_recovered_and_new_work_then_releases_only_held_queued_rows(
    db_session: AsyncSession,
) -> None:
    job, _actions, works = await _seed_job(
        db_session,
        [StoryArcSyncWorkState.COMPLETED, StoryArcSyncWorkState.QUEUED],
    )
    recovered, newly_journaled = works
    job.progress_snapshot = {"phase": "story_arcs"}
    recovered.claimable = True
    newly_journaled.claimable = False
    await db_session.flush()

    counts = await seal_import_story_arc_placement_origin(db_session, job.id)

    assert counts.total == 2
    assert counts.completed == 1
    assert counts.queued == 1
    assert recovered.claimable is True
    assert newly_journaled.claimable is True


@pytest.mark.asyncio
async def test_seal_rejects_held_work_that_is_no_longer_queued(
    db_session: AsyncSession,
) -> None:
    job, _actions, works = await _seed_job(
        db_session,
        [StoryArcSyncWorkState.RUNNING],
    )
    job.progress_snapshot = {"phase": "story_arcs"}
    works[0].claimable = False
    await db_session.flush()

    with pytest.raises(
        ValidationError,
        match="Held import Story Arc work changed before it was sealed",
    ):
        await seal_import_story_arc_placement_origin(db_session, job.id)

    assert works[0].claimable is False


@pytest.mark.asyncio
@pytest.mark.parametrize("held_work_count", [0, 1])
async def test_committed_cancel_wins_final_seal_and_runs_automatic_rollback(
    tmp_path: Path,
    held_work_count: int,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cancel-seal-race.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as seed_session:
            job, _actions, works = await _seed_job(
                seed_session,
                [StoryArcSyncWorkState.QUEUED] * held_work_count,
            )
            job.import_started_at = _COMPLETION_TIME
            job.progress_snapshot = {
                "status": ImportJobStatus.IMPORTING.value,
                "mode": "import",
                "phase": "story_arcs",
                "progress": 98,
            }
            for work in works:
                work.claimable = False
            await seed_session.commit()
            job_id = int(job.id)
            work_ids = [int(work.id) for work in works]

        mock_service = AsyncMock()

        async def lose_to_committed_cancel(session, current_job_id, progress_callback=None):
            # Model the last durable story-arc checkpoint: the worker keeps its
            # ORM identity but begins the seal in a new transaction.
            await session.commit()
            async with session_factory() as control_session:
                cancelled = await request_cancel(
                    control_session,
                    current_job_id,
                    log_event=AsyncMock(),
                )
                assert cancelled.status is ImportJobStatus.CANCELLING
                await control_session.commit()
            await seal_import_story_arc_placement_origin(session, current_job_id)
            raise AssertionError("A committed cancel must fence the final seal")

        async def finish_rollback(session, current_job_id, progress_callback=None):
            cancelled_job = await session.get(ImportJob, current_job_id)
            assert cancelled_job is not None
            assert cancelled_job.status is ImportJobStatus.ROLLING_BACK
            cancelled_job.status = ImportJobStatus.CANCELLED
            cancelled_job.control_request = ImportControlRequest.NONE
            cancelled_job.progress_snapshot = {
                "status": ImportJobStatus.CANCELLED.value,
                "mode": "import",
                "phase": "done",
                "progress": 100,
                "message": "Import cancelled by user.",
            }
            await session.flush()
            return True

        mock_service.run_import.side_effect = lose_to_committed_cancel
        mock_service.rollback_import.side_effect = finish_rollback
        runner = ImportRunner(session_factory)
        runner._publish_final_state = AsyncMock(return_value=ImportJobStatus.CANCELLED)

        with patch(
            "pullbox.tasks.import_task._build_import_service",
            new=AsyncMock(return_value=mock_service),
        ):
            await runner._run_job(job_id)

        mock_service.rollback_import.assert_awaited_once()
        async with session_factory() as observer:
            durable_job = await observer.get(ImportJob, job_id)
            assert durable_job is not None
            assert durable_job.status is ImportJobStatus.CANCELLED
            assert durable_job.progress_snapshot["phase"] == "done"
            for work_id in work_ids:
                held_work = await observer.get(StoryArcSyncWork, work_id)
                assert held_work is not None
                assert held_work.claimable is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("held_work_count", [0, 1])
async def test_committed_pause_wins_final_seal_without_publishing_work(
    tmp_path: Path,
    held_work_count: int,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pause-seal-race.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as seed_session:
            job, _actions, works = await _seed_job(
                seed_session,
                [StoryArcSyncWorkState.QUEUED] * held_work_count,
            )
            job.import_started_at = _COMPLETION_TIME
            job.progress_snapshot = {
                "status": ImportJobStatus.IMPORTING.value,
                "mode": "import",
                "phase": "story_arcs",
                "progress": 98,
            }
            for work in works:
                work.claimable = False
            await seed_session.commit()
            job_id = int(job.id)
            work_ids = [int(work.id) for work in works]

        mock_service = AsyncMock()

        async def lose_to_committed_pause(session, current_job_id, progress_callback=None):
            await session.commit()
            async with session_factory() as control_session:
                paused = await pause_job(
                    control_session,
                    current_job_id,
                    log_event=AsyncMock(),
                )
                assert paused.status is ImportJobStatus.PAUSED
                await control_session.commit()
            await seal_import_story_arc_placement_origin(session, current_job_id)
            raise AssertionError("A committed pause must fence the final seal")

        mock_service.run_import.side_effect = lose_to_committed_pause
        runner = ImportRunner(session_factory)
        runner._publish_final_state = AsyncMock(return_value=ImportJobStatus.PAUSED)

        with patch(
            "pullbox.tasks.import_task._build_import_service",
            new=AsyncMock(return_value=mock_service),
        ):
            await runner._run_job(job_id)

        mock_service.rollback_import.assert_not_awaited()
        async with session_factory() as observer:
            durable_job = await observer.get(ImportJob, job_id)
            assert durable_job is not None
            assert durable_job.status is ImportJobStatus.PAUSED
            assert durable_job.progress_snapshot["phase"] == "story_arcs"
            for work_id in work_ids:
                held_work = await observer.get(StoryArcSyncWork, work_id)
                assert held_work is not None
                assert held_work.claimable is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["cancel", "pause"])
async def test_control_racing_a_winning_seal_reloads_the_published_wait_phase(
    tmp_path: Path,
    control: str,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / f'{control}-after-seal.db'}",
        connect_args={"timeout": 0.05},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as seed_session:
            job, _actions, works = await _seed_job(
                seed_session,
                [StoryArcSyncWorkState.QUEUED],
            )
            job.import_started_at = _COMPLETION_TIME
            job.progress_snapshot = {
                "status": ImportJobStatus.IMPORTING.value,
                "mode": "import",
                "phase": "story_arcs",
                "progress": 98,
            }
            works[0].claimable = False
            await seed_session.commit()
            job_id = int(job.id)
            work_id = int(works[0].id)

        async with session_factory() as worker:
            await worker.execute(text("PRAGMA busy_timeout=25"))
            await worker.commit()
            worker_job = await worker.get(ImportJob, job_id)
            assert worker_job is not None
            counts = await seal_import_story_arc_placement_origin(worker, job_id)
            worker_job.progress_snapshot = {
                **dict(worker_job.progress_snapshot or {}),
                "phase": "story_arc_placements",
                "progress": 99,
                "story_arc_placements_total": counts.total,
            }
            await worker.flush()
            worker_released = False

            async def commit_winning_worker(_delay: float) -> None:
                nonlocal worker_released
                if not worker_released:
                    worker_released = True
                    await worker.commit()

            async with session_factory() as control_session:
                await control_session.execute(text("PRAGMA busy_timeout=25"))
                await control_session.commit()
                with patch(
                    "pullbox.services.import_job_controls.asyncio.sleep",
                    side_effect=commit_winning_worker,
                ):
                    if control == "cancel":
                        controlled_job = await request_cancel(
                            control_session,
                            job_id,
                            log_event=AsyncMock(),
                        )
                        assert controlled_job.status is ImportJobStatus.ROLLING_BACK
                        await control_session.commit()
                    else:
                        with pytest.raises(ValidationError, match="cannot be paused"):
                            await pause_job(
                                control_session,
                                job_id,
                                log_event=AsyncMock(),
                            )
                        await control_session.rollback()
            assert worker_released is True

        async with session_factory() as observer:
            durable_job = await observer.get(ImportJob, job_id)
            durable_work = await observer.get(StoryArcSyncWork, work_id)
            assert durable_job is not None
            assert durable_work is not None
            assert durable_work.claimable is True
            if control == "cancel":
                assert durable_job.status is ImportJobStatus.ROLLING_BACK
                assert durable_job.progress_snapshot["phase"] == "queued"
            else:
                assert durable_job.status is ImportJobStatus.IMPORTING
                assert durable_job.progress_snapshot["phase"] == "story_arc_placements"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_wait_phase_and_claimability_become_visible_in_one_commit(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'seal.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as seed_session:
            job, _actions, works = await _seed_job(
                seed_session,
                [StoryArcSyncWorkState.QUEUED],
            )
            job.progress_snapshot = {
                "status": ImportJobStatus.IMPORTING.value,
                "phase": "story_arcs",
                "progress": 98,
            }
            works[0].claimable = False
            await seed_session.commit()
            job_id = int(job.id)
            work_id = int(works[0].id)

        async with session_factory() as writer:
            writer_job = await writer.get(ImportJob, job_id)
            assert writer_job is not None
            counts = await seal_import_story_arc_placement_origin(writer, job_id)
            writer_job.progress_snapshot = {
                **dict(writer_job.progress_snapshot or {}),
                "phase": "story_arc_placements",
                "progress": 99,
                "story_arc_placements_total": counts.total,
            }
            await writer.flush()

            async with session_factory() as before_commit:
                hidden_job = await before_commit.get(ImportJob, job_id)
                hidden_work = await before_commit.get(StoryArcSyncWork, work_id)
                assert hidden_job is not None
                assert hidden_work is not None
                assert hidden_job.progress_snapshot["phase"] == "story_arcs"
                assert hidden_work.claimable is False

            await writer.commit()

        async with session_factory() as after_commit:
            visible_job = await after_commit.get(ImportJob, job_id)
            visible_work = await after_commit.get(StoryArcSyncWork, work_id)
            assert visible_job is not None
            assert visible_work is not None
            assert visible_job.progress_snapshot["phase"] == "story_arc_placements"
            assert visible_job.progress_snapshot["story_arc_placements_total"] == 1
            assert visible_work.claimable is True
    finally:
        await engine.dispose()
