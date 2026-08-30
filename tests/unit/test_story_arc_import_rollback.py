"""Story-arc journal rollback is ownership-aware and canonical-file safe."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from pullbox.models.import_job import (
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
    StoryArcExternalIdentity,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.services.import_job_actions import record_action, rollback_action
from pullbox.services.import_rollback_state import restore_review_state_after_rollback
from pullbox.services.import_story_arc_materialization import (
    materialize_confirmed_story_arcs,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _job(session: AsyncSession) -> ImportJob:
    job = ImportJob(
        source_path="/private/source",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.IMPORTING,
    )
    session.add(job)
    await session.flush()
    return job


async def _canonical_issue_and_file(
    session: AsyncSession,
) -> tuple[Issue, LibraryFile]:
    root = LibraryRoot(name="Library", path="/canonical/library")
    series = Series(title="Canonical", sort_title="canonical")
    session.add_all([root, series])
    await session.flush()
    issue = Issue(
        series_id=series.id,
        issue_number=1_000_000.0,
        issue_number_text="1000000",
    )
    session.add(issue)
    await session.flush()
    library_file = LibraryFile(
        file_path="/canonical/library/Canonical/Canonical 1000000.cbz",
        file_name="Canonical 1000000.cbz",
        file_size=123,
        file_format=FileFormat.CBZ,
        file_modified_at=datetime.now(UTC),
        match_confidence=MatchConfidence.HIGH,
        issue_id=issue.id,
        library_root_id=root.id,
    )
    session.add(library_file)
    await session.flush()
    return issue, library_file


async def _confirmed_arc(
    session: AsyncSession,
    *,
    job: ImportJob,
    name: str,
    proposed_story_arc_id: int | None = None,
    policy: dict[str, object] | None = None,
) -> ImportedStoryArc:
    staged = ImportedStoryArc(
        import_job_id=job.id,
        source_kind=(
            StoryArcSourceKind.FOLDER
            if proposed_story_arc_id is not None
            else StoryArcSourceKind.MYLAR3
        ),
        source_key=f"source:{name}",
        source_arc_id=None if proposed_story_arc_id is not None else f"source-{name}",
        source_ordinal=1,
        name=name,
        status=ImportedStoryArcStatus.CONFIRMED,
        selected_for_import=True,
        proposed_story_arc_id=proposed_story_arc_id,
        proposed_policy_snapshot=policy or {},
    )
    session.add(staged)
    await session.flush()
    return staged


async def _rollback_story_arc_actions(
    session: AsyncSession,
    *,
    job_id: int,
) -> list[ImportJobAction]:
    actions = list(
        (
            await session.scalars(
                select(ImportJobAction)
                .where(ImportJobAction.import_job_id == job_id)
                .order_by(ImportJobAction.sequence_no.desc())
            )
        ).all()
    )
    delete_series = AsyncMock()
    for action in actions:
        await rollback_action(
            session,
            action_id=action.id,
            action_type=action.action_type,
            payload=dict(action.payload or {}),
            delete_series=delete_series,
        )
    delete_series.assert_not_awaited()
    return actions


@pytest.mark.asyncio
async def test_created_arc_rollback_keeps_canonical_issue_and_library_file(
    db_session: AsyncSession,
) -> None:
    job = await _job(db_session)
    issue, library_file = await _canonical_issue_and_file(db_session)
    staged = await _confirmed_arc(db_session, job=job, name="Rollback Arc")
    entry = ImportedStoryArcEntry(
        imported_story_arc_id=staged.id,
        matched_issue_id=issue.id,
        source_ordinal=1,
        reading_order=9,
        reading_order_raw="009",
        resolution_state=StoryArcResolutionState.RESOLVED,
        source_kind=StoryArcSourceKind.MYLAR3,
        source_entry_id="entry-1",
        source_arc_id=staged.source_arc_id,
        source_issue_number_text="1000000",
        selected_for_import=True,
    )
    db_session.add(entry)
    await db_session.flush()
    await materialize_confirmed_story_arcs(
        db_session,
        import_job_id=job.id,
        record_action=record_action,
    )

    actions = await _rollback_story_arc_actions(db_session, job_id=job.id)
    await restore_review_state_after_rollback(db_session, job.id)

    assert [action.action_type for action in reversed(actions)] == [
        "story_arc_created",
        "story_arc_external_identity_created",
        "story_arc_membership_created",
    ]
    assert all(action.status == ImportJobActionStatus.ROLLED_BACK for action in actions)
    assert await db_session.scalar(select(func.count()).select_from(StoryArc)) == 0
    assert await db_session.scalar(select(func.count()).select_from(IssueStoryArc)) == 0
    assert await db_session.scalar(select(func.count()).select_from(StoryArcExternalIdentity)) == 0
    assert await db_session.get(Issue, issue.id) is issue
    assert await db_session.get(LibraryFile, library_file.id) is library_file
    assert staged.status == ImportedStoryArcStatus.CONFIRMED
    assert staged.materialized_story_arc_id is None
    assert entry.materialized_membership_id is None


@pytest.mark.asyncio
async def test_merged_arc_policy_rollback_restores_exact_prior_state(
    db_session: AsyncSession,
) -> None:
    job = await _job(db_session)
    existing = StoryArc(name="Existing", description="User arc")
    db_session.add(existing)
    await db_session.flush()
    before_revision = existing.revision
    staged = await _confirmed_arc(
        db_session,
        job=job,
        name="Existing",
        proposed_story_arc_id=existing.id,
        policy={
            "schema_version": 1,
            "source": "folder",
            "activation": "confirmed",
            "monitored": True,
            "search_missing": True,
            "include_upcoming": False,
            "sync_enabled": True,
        },
    )
    await materialize_confirmed_story_arcs(
        db_session,
        import_job_id=job.id,
        record_action=record_action,
    )
    action = (
        await db_session.scalars(
            select(ImportJobAction).where(ImportJobAction.action_type == "story_arc_policy_updated")
        )
    ).one()
    assert existing.monitored is True

    await rollback_action(
        db_session,
        action_id=action.id,
        action_type=action.action_type,
        payload=dict(action.payload or {}),
        delete_series=AsyncMock(),
    )

    assert existing.monitored is False
    assert existing.search_missing is False
    assert existing.sync_enabled is False
    assert existing.policy_schema_version is None
    assert existing.policy_snapshot == {}
    assert existing.revision == before_revision
    assert staged.materialized_story_arc_id == existing.id


@pytest.mark.asyncio
async def test_merged_membership_update_rollback_restores_unresolved_state(
    db_session: AsyncSession,
) -> None:
    job = await _job(db_session)
    issue, library_file = await _canonical_issue_and_file(db_session)
    existing = StoryArc(name="Existing Membership")
    db_session.add(existing)
    await db_session.flush()
    membership = IssueStoryArc(
        story_arc_id=existing.id,
        issue_id=None,
        sequence_number=4,
        source_ordinal=4,
        resolution_state=StoryArcResolutionState.PENDING,
        source_kind=StoryArcSourceKind.FOLDER,
        source_entry_id="entry-existing",
        source_issue_number_text="1000000",
        sync_eligible=False,
    )
    db_session.add(membership)
    await db_session.flush()
    revision_before = existing.revision
    staged = await _confirmed_arc(
        db_session,
        job=job,
        name="Existing Membership",
        proposed_story_arc_id=existing.id,
    )
    entry = ImportedStoryArcEntry(
        imported_story_arc_id=staged.id,
        matched_issue_id=issue.id,
        source_ordinal=4,
        reading_order=4,
        resolution_state=StoryArcResolutionState.RESOLVED,
        source_kind=StoryArcSourceKind.FOLDER,
        source_entry_id="entry-existing",
        source_issue_number_text="1000000",
        selected_for_import=True,
    )
    db_session.add(entry)
    await db_session.flush()

    await materialize_confirmed_story_arcs(
        db_session,
        import_job_id=job.id,
        record_action=record_action,
    )
    action = (
        await db_session.scalars(
            select(ImportJobAction).where(
                ImportJobAction.action_type == "story_arc_membership_updated"
            )
        )
    ).one()
    assert membership.issue_id == issue.id
    assert membership.resolution_state == StoryArcResolutionState.RESOLVED

    await rollback_action(
        db_session,
        action_id=action.id,
        action_type=action.action_type,
        payload=dict(action.payload or {}),
        delete_series=AsyncMock(),
    )

    assert membership.issue_id is None
    assert membership.resolution_state == StoryArcResolutionState.PENDING
    assert membership.sync_eligible is False
    assert existing.revision == revision_before
    assert await db_session.get(Issue, issue.id) is issue
    assert await db_session.get(LibraryFile, library_file.id) is library_file


@pytest.mark.asyncio
async def test_user_edited_merged_arc_causes_fail_safe_rollback_conflict(
    db_session: AsyncSession,
) -> None:
    job = await _job(db_session)
    existing = StoryArc(name="Existing")
    db_session.add(existing)
    await db_session.flush()
    await _confirmed_arc(
        db_session,
        job=job,
        name="Existing",
        proposed_story_arc_id=existing.id,
        policy={
            "schema_version": 1,
            "source": "folder",
            "activation": "confirmed",
            "monitored": True,
        },
    )
    await materialize_confirmed_story_arcs(
        db_session,
        import_job_id=job.id,
        record_action=record_action,
    )
    action = (
        await db_session.scalars(
            select(ImportJobAction).where(ImportJobAction.action_type == "story_arc_policy_updated")
        )
    ).one()
    existing.monitored = False
    await db_session.flush()

    with pytest.raises(ValueError, match="changed after import"):
        await rollback_action(
            db_session,
            action_id=action.id,
            action_type=action.action_type,
            payload=dict(action.payload or {}),
            delete_series=AsyncMock(),
        )

    assert action.status == ImportJobActionStatus.COMPLETED
    assert existing.monitored is False
