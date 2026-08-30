"""Step 4 materializes arcs without archive-content reads or source mutations."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobAction,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryRoot
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    ImportedStoryArcStatus,
    IssueStoryArc,
    StoryArc,
    StoryArcExternalIdentity,
    StoryArcPlacement,
    StoryArcPlacementMode,
    StoryArcPlacementOwnership,
    StoryArcPlacementState,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.services import import_story_arc_materialization as materialization_module
from pullbox.services.import_job_actions import record_action
from pullbox.services.import_story_arc_materialization import (
    materialize_confirmed_story_arcs,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _add_job(
    session: AsyncSession,
    *,
    source_type: ImportSourceType = ImportSourceType.MYLAR3,
    source_path: str = "/private/source",
    mylar3_path_map: dict[str, str] | None = None,
) -> ImportJob:
    job = ImportJob(
        source_path=source_path,
        source_type=source_type,
        status=ImportJobStatus.IMPORTING,
        mylar3_path_map=mylar3_path_map or {},
    )
    session.add(job)
    await session.flush()
    return job


async def _add_issue(
    session: AsyncSession,
    *,
    series_name: str,
    issue_number: float,
    issue_number_text: str,
) -> Issue:
    series = Series(title=series_name, sort_title=series_name)
    session.add(series)
    await session.flush()
    issue = Issue(
        series_id=series.id,
        issue_number=issue_number,
        issue_number_text=issue_number_text,
    )
    session.add(issue)
    await session.flush()
    return issue


async def _add_staged_arc(
    session: AsyncSession,
    *,
    job: ImportJob,
    name: str,
    source_key: str,
    source_arc_id: str | None,
    source_kind: StoryArcSourceKind = StoryArcSourceKind.MYLAR3,
    status: ImportedStoryArcStatus = ImportedStoryArcStatus.CONFIRMED,
    selected: bool = True,
    proposed_story_arc_id: int | None = None,
    policy: dict[str, object] | None = None,
) -> ImportedStoryArc:
    staged = ImportedStoryArc(
        import_job_id=job.id,
        source_kind=source_kind,
        source_key=source_key,
        source_arc_id=source_arc_id,
        source_ordinal=1,
        name=name,
        status=status,
        selected_for_import=selected,
        proposed_story_arc_id=proposed_story_arc_id,
        proposed_policy_snapshot=policy
        or {
            "schema_version": 1,
            "source": source_kind.value,
            "activation": "requires_confirmation",
        },
    )
    session.add(staged)
    await session.flush()
    return staged


async def _add_staged_entry(
    session: AsyncSession,
    *,
    staged_arc: ImportedStoryArc,
    source_ordinal: int,
    reading_order: int,
    issue_number_text: str,
    matched_issue_id: int | None = None,
    import_file_id: int | None = None,
    resolution_state: StoryArcResolutionState = StoryArcResolutionState.PENDING,
    source_entry_id: str | None = None,
    cv_arc_id: str | None = "4045-12",
    selected: bool = True,
    source_location: str | None = None,
) -> ImportedStoryArcEntry:
    entry = ImportedStoryArcEntry(
        imported_story_arc_id=staged_arc.id,
        import_file_id=import_file_id,
        matched_issue_id=matched_issue_id,
        source_ordinal=source_ordinal,
        reading_order=reading_order,
        reading_order_raw=str(reading_order),
        resolution_state=resolution_state,
        source_kind=staged_arc.source_kind,
        source_entry_id=source_entry_id or f"entry-{source_ordinal}",
        source_arc_id=staged_arc.source_arc_id,
        source_issue_id=f"issue-{source_ordinal}",
        source_series_id=f"series-{source_ordinal}",
        source_issue_number_text=issue_number_text,
        source_series_name=f"Series {source_ordinal}",
        source_issue_title=f"Issue {issue_number_text}",
        evidence={"schema_version": 1, "cv_arc_id": cv_arc_id},
        source_location=source_location,
        selected_for_import=selected,
    )
    session.add(entry)
    await session.flush()
    return entry


def _confirmed_policy(
    *,
    source: str,
    monitored: bool = True,
    search_missing: bool = True,
    include_upcoming: bool = False,
    mode: str = "logical",
    target_library_root_id: int | None = None,
    destination_root: str | None = None,
    synchronize: bool = False,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source": source,
        "activation": "confirmed",
        "monitored": monitored,
        "search_missing": search_missing,
        "include_upcoming": include_upcoming,
        "sync_enabled": synchronize,
        "placement_policy": {
            "schema_version": 1,
            "mode": mode,
            "target_library_root_id": target_library_root_id,
            "destination_root": destination_root,
            "folder_template": "{StoryArc}",
            "file_template": "{ReadingOrder:03d} - {Series} {IssueNumber}",
            "symlink_style": None,
            "synchronize": synchronize,
        },
    }


@pytest.mark.asyncio
async def test_materializes_exact_ordered_entries_identities_and_multiple_arcs(
    db_session: AsyncSession,
) -> None:
    job = await _add_job(db_session)
    policy_root = LibraryRoot(name="Arc Policy", path="/canonical/arc-policy", enabled=True)
    db_session.add(policy_root)
    await db_session.flush()
    million = await _add_issue(
        db_session,
        series_name="Million",
        issue_number=1_000_000.0,
        issue_number_text="1000000",
    )
    annual = await _add_issue(
        db_session,
        series_name="Annual",
        issue_number=1.0,
        issue_number_text="1AU",
    )
    policy = _confirmed_policy(
        source="mylar3",
        mode="copy",
        target_library_root_id=policy_root.id,
        destination_root="/canonical/story-arcs",
        synchronize=True,
    )
    first_arc = await _add_staged_arc(
        db_session,
        job=job,
        name="Synthetic Crossover",
        source_key="mylar3:first",
        source_arc_id="arc-local-1",
        policy=policy,
    )
    first = await _add_staged_entry(
        db_session,
        staged_arc=first_arc,
        source_ordinal=1,
        reading_order=2,
        issue_number_text="1000000",
        matched_issue_id=million.id,
    )
    second = await _add_staged_entry(
        db_session,
        staged_arc=first_arc,
        source_ordinal=2,
        reading_order=7,
        issue_number_text="1AU",
        matched_issue_id=annual.id,
    )
    missing = await _add_staged_entry(
        db_session,
        staged_arc=first_arc,
        source_ordinal=3,
        reading_order=7,
        issue_number_text="0.5",
        resolution_state=StoryArcResolutionState.MISSING,
    )
    duplicate = await _add_staged_entry(
        db_session,
        staged_arc=first_arc,
        source_ordinal=4,
        reading_order=11,
        issue_number_text="1000000",
        matched_issue_id=million.id,
    )
    second_arc = await _add_staged_arc(
        db_session,
        job=job,
        name="Second Arc",
        source_key="mylar3:second",
        source_arc_id="arc-local-2",
    )
    shared = await _add_staged_entry(
        db_session,
        staged_arc=second_arc,
        source_ordinal=1,
        reading_order=3,
        issue_number_text="1000000",
        matched_issue_id=million.id,
        cv_arc_id=None,
    )

    result = await materialize_confirmed_story_arcs(db_session, import_job_id=job.id)

    arcs = list((await db_session.scalars(select(StoryArc).order_by(StoryArc.id))).all())
    memberships = list(
        (
            await db_session.scalars(
                select(IssueStoryArc).order_by(
                    IssueStoryArc.story_arc_id,
                    IssueStoryArc.sequence_number,
                    IssueStoryArc.source_ordinal,
                )
            )
        ).all()
    )
    identities = list(
        (
            await db_session.scalars(
                select(StoryArcExternalIdentity).order_by(
                    StoryArcExternalIdentity.source,
                    StoryArcExternalIdentity.external_id,
                )
            )
        ).all()
    )

    assert result.arcs_created == 2
    assert result.memberships_created == 4
    assert result.memberships_reused == 1
    assert result.resolved_entries == 4
    assert result.unresolved_entries == 1
    assert {warning.code for warning in result.warnings} == {
        "duplicate_issue_membership_reused",
        "policy_not_activated",
    }
    assert len(arcs) == 2
    assert arcs[0].monitored is True
    assert arcs[0].search_missing is True
    assert arcs[0].include_upcoming is False
    assert arcs[0].sync_enabled is True
    assert arcs[0].target_library_root_id == policy_root.id
    assert arcs[0].policy_schema_version == 1
    assert arcs[0].policy_snapshot == policy["placement_policy"]
    assert [item.sequence_number for item in memberships if item.story_arc_id == arcs[0].id] == [
        2,
        7,
        7,
    ]
    assert [
        item.source_issue_number_text for item in memberships if item.story_arc_id == arcs[0].id
    ] == ["1000000", "1AU", "0.5"]
    assert [item.resolution_state for item in memberships if item.story_arc_id == arcs[0].id] == [
        StoryArcResolutionState.RESOLVED,
        StoryArcResolutionState.RESOLVED,
        StoryArcResolutionState.MISSING,
    ]
    assert {item.issue_id for item in memberships if item.issue_id == million.id} == {million.id}
    assert {(item.source, item.namespace, item.external_id) for item in identities} == {
        ("comicvine", "story_arc", "4045-12"),
        ("mylar3", "story_arc", "arc-local-1"),
        ("mylar3", "story_arc", "arc-local-2"),
    }
    assert first.materialized_membership_id is not None
    assert second.materialized_membership_id is not None
    assert missing.materialized_membership_id is not None
    assert duplicate.materialized_membership_id == first.materialized_membership_id
    assert shared.materialized_membership_id is not None
    assert first_arc.status == ImportedStoryArcStatus.IMPORTED
    assert second_arc.status == ImportedStoryArcStatus.IMPORTED
    assert first_arc.materialized_story_arc_id == arcs[0].id
    assert first_arc.diagnostics["materialization"]["counts"] == {
        "arcs_created": 1,
        "arcs_merged": 0,
        "arcs_reused": 0,
        "arcs_failed": 0,
        "external_identities_created": 2,
        "external_identities_reused": 0,
        "memberships_created": 3,
        "memberships_reused": 1,
        "resolved_entries": 3,
        "unresolved_entries": 1,
        "entries_skipped": 0,
    }
    assert await db_session.scalar(select(func.count()).select_from(StoryArcPlacement)) == 0


@pytest.mark.asyncio
async def test_explicit_merge_uses_safe_imported_file_match_and_unconfirmed_policy_is_inert(
    db_session: AsyncSession,
) -> None:
    job = await _add_job(db_session, source_type=ImportSourceType.FILESYSTEM)
    issue = await _add_issue(
        db_session,
        series_name="Folder Series",
        issue_number=0.5,
        issue_number_text="0.5",
    )
    existing = StoryArc(name="Existing Arc")
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Folder Series",
        file_count=1,
    )
    db_session.add_all([existing, imported_series])
    await db_session.flush()
    imported_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=imported_series.id,
        file_path="/private/source/Folder Series 0.5.cbz",
        file_name="Folder Series 0.5.cbz",
        file_size=100,
        file_format="cbz",
        issue_number_raw="0.5",
        matched_issue_id=issue.id,
        status=ImportedFileStatus.IMPORTED,
    )
    db_session.add(imported_file)
    await db_session.flush()
    staged = await _add_staged_arc(
        db_session,
        job=job,
        name="Ignored Rename",
        source_key="folder:one",
        source_arc_id=None,
        source_kind=StoryArcSourceKind.FOLDER,
        proposed_story_arc_id=existing.id,
        policy={
            "schema_version": 1,
            "source": "folder",
            "activation": "requires_confirmation",
            "monitored": True,
            "sync_enabled": True,
        },
    )
    entry = await _add_staged_entry(
        db_session,
        staged_arc=staged,
        source_ordinal=1,
        reading_order=5,
        issue_number_text="0.5",
        import_file_id=imported_file.id,
        cv_arc_id=None,
    )

    result = await materialize_confirmed_story_arcs(db_session, import_job_id=job.id)

    membership = (await db_session.scalars(select(IssueStoryArc))).one()
    assert result.arcs_created == 0
    assert result.arcs_merged == 1
    assert result.resolved_entries == 1
    assert {warning.code for warning in result.warnings} == {"policy_not_activated"}
    assert await db_session.scalar(select(func.count()).select_from(StoryArc)) == 1
    assert existing.name == "Existing Arc"
    assert existing.monitored is False
    assert existing.sync_enabled is False
    assert membership.story_arc_id == existing.id
    assert membership.issue_id == issue.id
    assert entry.materialized_membership_id == membership.id


@pytest.mark.asyncio
async def test_confirmed_policy_rejects_noncanonical_or_mismatched_envelopes(
    db_session: AsyncSession,
) -> None:
    job = await _add_job(db_session)
    malformed_policies = [
        {
            **_confirmed_policy(source="mylar3"),
            "unexpected": True,
        },
        {
            **_confirmed_policy(source="mylar3"),
            "placement_policy": {
                **_confirmed_policy(source="mylar3")["placement_policy"],
                "unexpected": True,
            },
        },
        {
            **_confirmed_policy(source="mylar3"),
            "sync_enabled": True,
        },
        {
            **_confirmed_policy(source="mylar3"),
            "monitored": False,
        },
        _confirmed_policy(
            source="mylar3",
            mode="copy",
            target_library_root_id=987_654,
            destination_root="/missing/story-arcs",
        ),
    ]
    staged_arcs: list[ImportedStoryArc] = []
    for ordinal, policy in enumerate(malformed_policies, start=1):
        staged_arcs.append(
            await _add_staged_arc(
                db_session,
                job=job,
                name=f"Rejected Policy {ordinal}",
                source_key=f"mylar3:rejected-policy:{ordinal}",
                source_arc_id=f"rejected-policy-{ordinal}",
                policy=policy,
            )
        )

    result = await materialize_confirmed_story_arcs(
        db_session,
        import_job_id=job.id,
    )

    warning_codes = [warning.code for warning in result.warnings]
    assert warning_codes.count("policy_validation_failed") == 4
    assert warning_codes.count("policy_target_root_missing") == 1
    arcs = list((await db_session.scalars(select(StoryArc).order_by(StoryArc.id))).all())
    assert len(arcs) == len(staged_arcs)
    assert all(arc.monitored is False for arc in arcs)
    assert all(arc.search_missing is False for arc in arcs)
    assert all(arc.sync_enabled is False for arc in arcs)
    assert all(arc.target_library_root_id is None for arc in arcs)
    assert all(arc.policy_schema_version is None for arc in arcs)
    assert all(arc.policy_snapshot == {} for arc in arcs)


@pytest.mark.asyncio
async def test_restart_repairs_missing_materialized_pointers_without_duplicate_rows(
    db_session: AsyncSession,
) -> None:
    job = await _add_job(db_session)
    issue = await _add_issue(
        db_session,
        series_name="Restart",
        issue_number=1.0,
        issue_number_text="1AU",
    )
    staged = await _add_staged_arc(
        db_session,
        job=job,
        name="Restart Arc",
        source_key="mylar3:restart",
        source_arc_id="restart-arc",
    )
    entry = await _add_staged_entry(
        db_session,
        staged_arc=staged,
        source_ordinal=1,
        reading_order=10,
        issue_number_text="1AU",
        matched_issue_id=issue.id,
    )
    first = await materialize_confirmed_story_arcs(db_session, import_job_id=job.id)
    arc_id = staged.materialized_story_arc_id
    membership_id = entry.materialized_membership_id
    staged.status = ImportedStoryArcStatus.CONFIRMED
    staged.materialized_story_arc_id = None
    entry.materialized_membership_id = None
    await db_session.flush()

    second = await materialize_confirmed_story_arcs(db_session, import_job_id=job.id)

    assert first.arcs_created == 1
    assert second.arcs_created == 0
    assert second.arcs_reused == 1
    assert second.memberships_created == 0
    assert second.memberships_reused == 1
    assert staged.materialized_story_arc_id == arc_id
    assert entry.materialized_membership_id == membership_id
    assert await db_session.scalar(select(func.count()).select_from(StoryArc)) == 1
    assert await db_session.scalar(select(func.count()).select_from(IssueStoryArc)) == 1
    assert await db_session.scalar(select(func.count()).select_from(StoryArcExternalIdentity)) == 2


@pytest.mark.asyncio
async def test_existing_external_identity_requires_an_explicit_merge_decision(
    db_session: AsyncSession,
) -> None:
    job = await _add_job(db_session)
    existing = StoryArc(name="Already Imported")
    db_session.add(existing)
    await db_session.flush()
    db_session.add(
        StoryArcExternalIdentity(
            story_arc_id=existing.id,
            source="mylar3",
            namespace="story_arc",
            external_id="already-imported",
        )
    )
    staged = await _add_staged_arc(
        db_session,
        job=job,
        name="Would Be Implicit",
        source_key="mylar3:existing",
        source_arc_id="already-imported",
    )
    await _add_staged_entry(
        db_session,
        staged_arc=staged,
        source_ordinal=1,
        reading_order=1,
        issue_number_text="0.5",
        resolution_state=StoryArcResolutionState.MISSING,
        cv_arc_id=None,
    )

    result = await materialize_confirmed_story_arcs(db_session, import_job_id=job.id)

    assert result.arcs_created == 0
    assert result.arcs_merged == 0
    assert result.arcs_failed == 1
    assert staged.status == ImportedStoryArcStatus.FAILED
    assert staged.materialized_story_arc_id is None
    assert "external_identity_requires_explicit_merge_review" in {
        warning.code for warning in result.warnings
    }
    assert await db_session.scalar(select(func.count()).select_from(StoryArc)) == 1
    assert await db_session.scalar(select(func.count()).select_from(IssueStoryArc)) == 0


@pytest.mark.asyncio
async def test_unfinished_import_file_match_remains_unresolved(
    db_session: AsyncSession,
) -> None:
    job = await _add_job(db_session, source_type=ImportSourceType.FILESYSTEM)
    issue = await _add_issue(
        db_session,
        series_name="Not Finished",
        issue_number=1.0,
        issue_number_text="1",
    )
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Not Finished",
        file_count=1,
    )
    db_session.add(imported_series)
    await db_session.flush()
    imported_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=imported_series.id,
        file_path="/private/source/Not Finished 001.cbz",
        file_name="Not Finished 001.cbz",
        file_size=100,
        file_format="cbz",
        matched_issue_id=issue.id,
        status=ImportedFileStatus.MATCHED,
    )
    db_session.add(imported_file)
    await db_session.flush()
    staged = await _add_staged_arc(
        db_session,
        job=job,
        name="Unfinished File Arc",
        source_key="folder:unfinished",
        source_arc_id=None,
        source_kind=StoryArcSourceKind.FOLDER,
    )
    await _add_staged_entry(
        db_session,
        staged_arc=staged,
        source_ordinal=1,
        reading_order=1,
        issue_number_text="1",
        import_file_id=imported_file.id,
        cv_arc_id=None,
    )

    result = await materialize_confirmed_story_arcs(db_session, import_job_id=job.id)

    membership = (await db_session.scalars(select(IssueStoryArc))).one()
    assert membership.issue_id is None
    assert membership.resolution_state == StoryArcResolutionState.PENDING
    assert result.resolved_entries == 0
    assert result.unresolved_entries == 1
    assert "import_file_match_not_materialized" in {warning.code for warning in result.warnings}


@pytest.mark.asyncio
async def test_unconfirmed_step_three_rows_never_mutate_canonical_tables(
    db_session: AsyncSession,
) -> None:
    job = await _add_job(db_session)
    staged = await _add_staged_arc(
        db_session,
        job=job,
        name="Review Only",
        source_key="mylar3:review",
        source_arc_id="review-arc",
        status=ImportedStoryArcStatus.NEEDS_REVIEW,
        selected=False,
    )
    await _add_staged_entry(
        db_session,
        staged_arc=staged,
        source_ordinal=1,
        reading_order=1,
        issue_number_text="1000000",
        resolution_state=StoryArcResolutionState.MISSING,
    )

    result = await materialize_confirmed_story_arcs(db_session, import_job_id=job.id)

    assert result.arcs_examined == 0
    assert await db_session.scalar(select(func.count()).select_from(StoryArc)) == 0
    assert await db_session.scalar(select(func.count()).select_from(IssueStoryArc)) == 0
    assert staged.materialized_story_arc_id is None


@pytest.mark.asyncio
async def test_cancellation_is_checked_between_bounded_batches_and_service_never_commits(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = await _add_job(db_session)
    for ordinal in range(1, 3):
        staged = await _add_staged_arc(
            db_session,
            job=job,
            name=f"Arc {ordinal}",
            source_key=f"mylar3:{ordinal}",
            source_arc_id=f"arc-{ordinal}",
        )
        await _add_staged_entry(
            db_session,
            staged_arc=staged,
            source_ordinal=1,
            reading_order=ordinal,
            issue_number_text="0.5",
            resolution_state=StoryArcResolutionState.MISSING,
        )

    commit = AsyncMock(side_effect=AssertionError("materializer must not commit"))
    monkeypatch.setattr(db_session, "commit", commit)
    checkpoints = 0

    async def cancel_before_second_batch() -> None:
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints == 3:
            raise RuntimeError("synthetic cancellation")

    with pytest.raises(RuntimeError, match="synthetic cancellation"):
        await materialize_confirmed_story_arcs(
            db_session,
            import_job_id=job.id,
            batch_size=1,
            cancellation_check=cancel_before_second_batch,
        )

    assert checkpoints == 3
    commit.assert_not_awaited()
    assert await db_session.scalar(select(func.count()).select_from(StoryArc)) == 1


@pytest.mark.asyncio
async def test_actual_mutations_are_journaled_once_in_reverse_safe_order(
    db_session: AsyncSession,
) -> None:
    job = await _add_job(db_session)
    issue = await _add_issue(
        db_session,
        series_name="Journal",
        issue_number=1_000_000.0,
        issue_number_text="1000000",
    )
    staged = await _add_staged_arc(
        db_session,
        job=job,
        name="Journal Arc",
        source_key="mylar3:journal",
        source_arc_id="journal-arc",
    )
    await _add_staged_entry(
        db_session,
        staged_arc=staged,
        source_ordinal=1,
        reading_order=12,
        issue_number_text="1000000",
        matched_issue_id=issue.id,
    )

    await materialize_confirmed_story_arcs(
        db_session,
        import_job_id=job.id,
        record_action=record_action,
    )
    first_actions = list(
        (
            await db_session.scalars(select(ImportJobAction).order_by(ImportJobAction.sequence_no))
        ).all()
    )
    staged.status = ImportedStoryArcStatus.CONFIRMED
    await materialize_confirmed_story_arcs(
        db_session,
        import_job_id=job.id,
        record_action=record_action,
    )
    second_actions = list(
        (
            await db_session.scalars(select(ImportJobAction).order_by(ImportJobAction.sequence_no))
        ).all()
    )

    assert [action.action_type for action in first_actions] == [
        "story_arc_created",
        "story_arc_external_identity_created",
        "story_arc_external_identity_created",
        "story_arc_membership_created",
    ]
    assert [action.id for action in second_actions] == [action.id for action in first_actions]
    assert first_actions[-1].payload["expected_after"]["source_issue_number_text"] == ("1000000")


@pytest.mark.asyncio
async def test_existing_folder_arc_artifact_is_journaled_as_reference_without_content_io(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "approved-source"
    source_root.mkdir()
    artifact = source_root / "Imported Arc" / "001 - Existing.cbz"
    artifact.parent.mkdir()
    original_content = b"pre-existing-user-archive"
    artifact.write_bytes(original_content)
    job = await _add_job(
        db_session,
        source_type=ImportSourceType.FILESYSTEM,
        source_path=str(source_root),
    )
    staged = await _add_staged_arc(
        db_session,
        job=job,
        name="Imported Arc",
        source_key="folder:referenced",
        source_arc_id=None,
        source_kind=StoryArcSourceKind.FOLDER,
    )
    entry = await _add_staged_entry(
        db_session,
        staged_arc=staged,
        source_ordinal=1,
        reading_order=1,
        issue_number_text="1",
        cv_arc_id=None,
        source_location=str(artifact),
    )
    original_open = Path.open

    def refuse_archive_open(path: Path, *args: object, **kwargs: object) -> object:
        if path == artifact:
            raise AssertionError("story-arc reference materialization opened archive content")
        return original_open(path, *args, **kwargs)

    def refuse_descriptor_read(_descriptor: int, _size: int) -> bytes:
        raise AssertionError("story-arc reference materialization read archive content")

    monkeypatch.setattr(Path, "open", refuse_archive_open)
    monkeypatch.setattr(materialization_module.os, "read", refuse_descriptor_read)
    journal_observations: list[int] = []

    async def observe_record_action(*args: object, **kwargs: object) -> ImportJobAction:
        if kwargs.get("action_type") == "story_arc_referenced_placement_attached":
            journal_observations.append(
                int(
                    await db_session.scalar(select(func.count()).select_from(StoryArcPlacement))
                    or 0
                )
            )
        return await record_action(*args, **kwargs)  # type: ignore[arg-type]

    await materialize_confirmed_story_arcs(
        db_session,
        import_job_id=job.id,
        record_action=observe_record_action,
    )

    placement = (await db_session.scalars(select(StoryArcPlacement))).one()
    action = (
        await db_session.scalars(
            select(ImportJobAction).where(
                ImportJobAction.action_type == "story_arc_referenced_placement_attached"
            )
        )
    ).one()
    assert journal_observations == [0]
    assert placement.issue_story_arc_id == entry.materialized_membership_id
    assert placement.placement_path == str(artifact)
    assert placement.mode == StoryArcPlacementMode.REFERENCE_ONLY
    assert placement.ownership == StoryArcPlacementOwnership.REFERENCED
    assert placement.source_kind == StoryArcSourceKind.FOLDER
    assert placement.source_import_job_id == job.id
    assert placement.creating_action_id == action.id
    assert placement.state == StoryArcPlacementState.CURRENT
    assert placement.source_fingerprint == {}
    assert placement.last_result["code"] == "reference_current"
    assert (
        placement.last_result["baseline_fingerprint"]
        == placement.last_result["observed_fingerprint"]
    )
    assert placement.last_result["observed_fingerprint"]["size"] == len(original_content)
    assert action.payload["journal_state"] == "completed"
    assert action.payload["placement_id"] == placement.id
    with original_open(artifact, "rb") as stream:
        assert stream.read() == original_content


@pytest.mark.asyncio
async def test_mylar_reference_location_uses_the_confirmed_host_path_mapping(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    host_root = tmp_path / "host-comics"
    artifact = host_root / "Arc" / "Issue 001.cbr"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"existing-mylar-arc-copy")
    job = await _add_job(
        db_session,
        source_path=str(tmp_path / "mylar.db"),
        mylar3_path_map={"/comics": str(host_root)},
    )
    staged = await _add_staged_arc(
        db_session,
        job=job,
        name="Mylar Arc",
        source_key="mylar3:referenced",
        source_arc_id="mylar-arc",
    )
    await _add_staged_entry(
        db_session,
        staged_arc=staged,
        source_ordinal=1,
        reading_order=1,
        issue_number_text="1",
        cv_arc_id=None,
        source_location="/comics/Arc/Issue 001.cbr",
    )

    await materialize_confirmed_story_arcs(
        db_session,
        import_job_id=job.id,
        record_action=record_action,
    )

    placement = (await db_session.scalars(select(StoryArcPlacement))).one()
    assert placement.placement_path == str(artifact)
    assert placement.source_kind == StoryArcSourceKind.MYLAR3
    assert placement.source_import_job_id == job.id


@pytest.mark.asyncio
async def test_unsafe_or_missing_reference_locations_warn_without_ownership_claims(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "approved-source"
    source_root.mkdir()
    outside = tmp_path / "outside.cbz"
    outside.write_bytes(b"outside")
    directory = source_root / "not-a-file.cbz"
    directory.mkdir()
    real_parent = source_root / "real-parent"
    real_parent.mkdir()
    (real_parent / "issue.cbz").write_bytes(b"symlink-parent")
    symlink_parent = source_root / "linked-parent"
    symlink_parent.symlink_to(real_parent, target_is_directory=True)
    real_file = source_root / "real-file.cbz"
    real_file.write_bytes(b"symlink-file")
    symlink_file = source_root / "linked-file.cbz"
    symlink_file.symlink_to(real_file)
    job = await _add_job(
        db_session,
        source_type=ImportSourceType.FILESYSTEM,
        source_path=str(source_root),
    )
    locations = (
        str(tmp_path / "outside.cbz"),
        str(source_root / "missing.cbz"),
        str(directory),
        str(symlink_parent / "issue.cbz"),
        str(symlink_file),
    )
    entries: list[ImportedStoryArcEntry] = []
    for ordinal, location in enumerate(locations, start=1):
        staged = await _add_staged_arc(
            db_session,
            job=job,
            name=f"Unsafe {ordinal}",
            source_key=f"folder:unsafe:{ordinal}",
            source_arc_id=None,
            source_kind=StoryArcSourceKind.FOLDER,
        )
        entries.append(
            await _add_staged_entry(
                db_session,
                staged_arc=staged,
                source_ordinal=1,
                reading_order=ordinal,
                issue_number_text=str(ordinal),
                cv_arc_id=None,
                source_location=location,
            )
        )

    result = await materialize_confirmed_story_arcs(
        db_session,
        import_job_id=job.id,
        record_action=record_action,
    )

    reference_codes = {
        warning.code
        for warning in result.warnings
        if warning.code.startswith("story_arc_reference_")
    }
    assert reference_codes == {
        "story_arc_reference_missing",
        "story_arc_reference_not_regular_file",
        "story_arc_reference_outside_trusted_root",
        "story_arc_reference_symlink",
    }
    assert await db_session.scalar(select(func.count()).select_from(StoryArcPlacement)) == 0
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ImportJobAction)
            .where(ImportJobAction.action_type == "story_arc_referenced_placement_attached")
        )
        == 0
    )
    assert all(
        any(
            code.startswith("story_arc_reference_")
            for code in entry.diagnostics["materialization"]["warning_codes"]
        )
        for entry in entries
    )
    assert all(str(tmp_path) not in warning.code for warning in result.warnings)


@pytest.mark.asyncio
async def test_reference_retry_is_idempotent_and_reports_changed_artifact_as_drift(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "approved-source"
    source_root.mkdir()
    artifact = source_root / "Existing.cbz"
    artifact.write_bytes(b"original")
    job = await _add_job(
        db_session,
        source_type=ImportSourceType.FILESYSTEM,
        source_path=str(source_root),
    )
    staged = await _add_staged_arc(
        db_session,
        job=job,
        name="Retry Arc",
        source_key="folder:retry-reference",
        source_arc_id=None,
        source_kind=StoryArcSourceKind.FOLDER,
    )
    await _add_staged_entry(
        db_session,
        staged_arc=staged,
        source_ordinal=1,
        reading_order=1,
        issue_number_text="1",
        cv_arc_id=None,
        source_location=str(artifact),
    )
    await materialize_confirmed_story_arcs(
        db_session,
        import_job_id=job.id,
        record_action=record_action,
    )
    placement = (await db_session.scalars(select(StoryArcPlacement))).one()
    baseline = dict(placement.last_result["baseline_fingerprint"])
    first_action_id = placement.creating_action_id
    artifact.write_bytes(b"changed-and-longer")
    staged.status = ImportedStoryArcStatus.CONFIRMED
    await db_session.flush()

    result = await materialize_confirmed_story_arcs(
        db_session,
        import_job_id=job.id,
        record_action=record_action,
    )

    await db_session.refresh(placement)
    assert placement.state == StoryArcPlacementState.DRIFTED
    assert placement.last_result["code"] == "reference_drifted"
    assert placement.last_result["baseline_fingerprint"] == baseline
    assert placement.last_result["observed_fingerprint"]["size"] == len(b"changed-and-longer")
    assert placement.creating_action_id == first_action_id
    assert await db_session.scalar(select(func.count()).select_from(StoryArcPlacement)) == 1
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ImportJobAction)
            .where(ImportJobAction.action_type == "story_arc_referenced_placement_attached")
        )
        == 1
    )
    assert "story_arc_reference_drifted" in {warning.code for warning in result.warnings}

    artifact.unlink()
    staged.status = ImportedStoryArcStatus.CONFIRMED
    await db_session.flush()
    missing_result = await materialize_confirmed_story_arcs(
        db_session,
        import_job_id=job.id,
        record_action=record_action,
    )

    await db_session.refresh(placement)
    assert placement.state == StoryArcPlacementState.MISSING
    assert placement.last_result["code"] == "reference_missing"
    assert placement.last_result["baseline_fingerprint"] == baseline
    assert placement.last_result["observed_fingerprint"] is None
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ImportJobAction)
            .where(ImportJobAction.action_type == "story_arc_referenced_placement_attached")
        )
        == 1
    )
    assert "story_arc_reference_missing" in {warning.code for warning in missing_result.warnings}


@pytest.mark.asyncio
async def test_cancellation_checkpoint_after_membership_never_claims_reference_ownership(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "approved-source"
    source_root.mkdir()
    artifact = source_root / "Existing.cbz"
    original_content = b"untouched-on-cancel"
    artifact.write_bytes(original_content)
    job = await _add_job(
        db_session,
        source_type=ImportSourceType.FILESYSTEM,
        source_path=str(source_root),
    )
    staged = await _add_staged_arc(
        db_session,
        job=job,
        name="Cancel Arc",
        source_key="folder:cancel-reference",
        source_arc_id=None,
        source_kind=StoryArcSourceKind.FOLDER,
    )
    await _add_staged_entry(
        db_session,
        staged_arc=staged,
        source_ordinal=1,
        reading_order=1,
        issue_number_text="1",
        cv_arc_id=None,
        source_location=str(artifact),
    )

    async def cancel_after_membership() -> None:
        membership_count = int(
            await db_session.scalar(select(func.count()).select_from(IssueStoryArc)) or 0
        )
        if membership_count:
            raise RuntimeError("cancel before reference attachment")

    with pytest.raises(RuntimeError, match="cancel before reference attachment"):
        await materialize_confirmed_story_arcs(
            db_session,
            import_job_id=job.id,
            record_action=record_action,
            cancellation_check=cancel_after_membership,
        )

    assert await db_session.scalar(select(func.count()).select_from(IssueStoryArc)) == 1
    assert await db_session.scalar(select(func.count()).select_from(StoryArcPlacement)) == 0
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ImportJobAction)
            .where(ImportJobAction.action_type == "story_arc_referenced_placement_attached")
        )
        == 0
    )
    assert artifact.read_bytes() == original_content
