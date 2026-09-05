"""Tests for import Step 5 results context loading."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobAction,
    ImportJobActionStatus,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import IssueCatalogState, Series
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
from pullbox.services.import_safety_diagnostics import (
    ImportSafetyCategory,
    build_import_safety_diagnostics,
)


@pytest.mark.asyncio
async def test_load_import_results_context_splits_unmatched_queue_counts(db_session) -> None:  # type: ignore[no-untyped-def]
    from pullbox.ui.import_results_context import load_import_results_context

    job = ImportJob(
        source_path="/tmp/comics",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.COMPLETED,
        total_files_no_match=2,
        series_no_match=1,
    )
    db_session.add(job)
    await db_session.flush()

    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Imported Series",
        file_count=1,
        has_files=True,
        sample_paths=[],
        status=ImportSeriesStatus.IMPORTED,
    )
    unmatched_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Unmatched Series",
        file_count=1,
        has_files=True,
        sample_paths=[],
        status=ImportSeriesStatus.NO_MATCH,
    )
    db_session.add_all([imported_series, unmatched_series])
    await db_session.flush()

    db_session.add_all(
        [
            ImportedFile(
                import_job_id=job.id,
                import_series_id=imported_series.id,
                file_path="/tmp/comics/imported-no-match.cbz",
                file_name="imported-no-match.cbz",
                file_size=1024,
                file_format="cbz",
                status=ImportedFileStatus.NO_MATCH,
            ),
            ImportedFile(
                import_job_id=job.id,
                import_series_id=unmatched_series.id,
                file_path="/tmp/comics/unmatched-series-file.cbz",
                file_name="unmatched-series-file.cbz",
                file_size=1024,
                file_format="cbz",
                status=ImportedFileStatus.NO_MATCH,
            ),
        ]
    )
    await db_session.flush()

    context = await load_import_results_context(db_session, job)

    assert context["files_no_match"] == 2
    assert context["orphaned_file_no_match_count"] == 1
    assert context["identified_series_file_no_match_count"] == 1
    assert context["no_match_count"] == 1
    assert context["unmatched_queue_count"] == 2


@pytest.mark.asyncio
async def test_load_import_results_context_reports_changed_sources_separately(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    from pullbox.ui.import_results_context import load_import_results_context

    job = ImportJob(
        source_path="/tmp/comics",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.COMPLETED,
        total_files_found=2,
        total_files_failed=2,
    )
    db_session.add(job)
    await db_session.flush()

    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Changed Sources",
        file_count=2,
        has_files=True,
        sample_paths=[],
        status=ImportSeriesStatus.FAILED,
    )
    db_session.add(imported_series)
    await db_session.flush()
    db_session.add_all(
        [
            ImportedFile(
                import_job_id=job.id,
                import_series_id=imported_series.id,
                file_path="/tmp/comics/changed.cbz",
                file_name="changed.cbz",
                file_size=1024,
                file_format="cbz",
                status=ImportedFileStatus.FAILED,
                diagnostics={
                    "source_revalidation": {
                        "code": "source_changed",
                        "retryable": True,
                    }
                },
            ),
            ImportedFile(
                import_job_id=job.id,
                import_series_id=imported_series.id,
                file_path="/tmp/comics/failed.cbz",
                file_name="failed.cbz",
                file_size=1024,
                file_format="cbz",
                status=ImportedFileStatus.FAILED,
                error_message="Destination is unavailable.",
            ),
        ]
    )
    await db_session.flush()

    context = await load_import_results_context(db_session, job)

    assert context["files_total"] == 2
    assert context["files_failed"] == 2
    assert context["source_changed_files"] == 1


@pytest.mark.asyncio
async def test_load_import_results_context_counts_pending_catalog_sync(db_session) -> None:  # type: ignore[no-untyped-def]
    from pullbox.ui.import_results_context import load_import_results_context

    job = ImportJob(
        source_path="/tmp/comics",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.COMPLETED,
    )
    db_session.add(job)
    await db_session.flush()

    hydrating = Series(
        title="Still Syncing",
        sort_title="Still Syncing",
        monitored=True,
        issue_catalog_state=IssueCatalogState.HYDRATING,
    )
    failed = Series(
        title="Needs Retry",
        sort_title="Needs Retry",
        monitored=True,
        issue_catalog_state=IssueCatalogState.FAILED,
        issue_catalog_error="ComicVine timed out",
    )
    complete = Series(
        title="Already Complete",
        sort_title="Already Complete",
        monitored=True,
        issue_catalog_state=IssueCatalogState.COMPLETE,
    )
    db_session.add_all([hydrating, failed, complete])
    await db_session.flush()

    db_session.add_all(
        [
            ImportedSeries(
                import_job_id=job.id,
                raw_series_name="Still Syncing",
                file_count=1,
                has_files=True,
                sample_paths=[],
                status=ImportSeriesStatus.IMPORTED,
                series_id=hydrating.id,
            ),
            ImportedSeries(
                import_job_id=job.id,
                raw_series_name="Needs Retry",
                file_count=1,
                has_files=True,
                sample_paths=[],
                status=ImportSeriesStatus.IMPORTED,
                series_id=failed.id,
            ),
            ImportedSeries(
                import_job_id=job.id,
                raw_series_name="Already Complete",
                file_count=1,
                has_files=True,
                sample_paths=[],
                status=ImportSeriesStatus.IMPORTED,
                series_id=complete.id,
            ),
        ]
    )
    await db_session.flush()

    context = await load_import_results_context(db_session, job)

    assert context["catalog_sync_pending_count"] == 1
    assert context["catalog_sync_failed_count"] == 1
    assert context["catalog_sync_attention_count"] == 2
    assert [item.title for item in context["catalog_sync_series"]] == [
        "Needs Retry",
        "Still Syncing",
    ]


@pytest.mark.asyncio
async def test_load_import_results_context_summarizes_rollback_journal_ownership(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    from pullbox.ui.import_results_context import load_import_results_context

    job = ImportJob(
        source_path="/tmp/comics",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.COMPLETED,
        total_files_imported=4,
        total_files_already_owned=1,
    )
    db_session.add(job)
    await db_session.flush()

    db_session.add_all(
        [
            ImportJobAction(
                import_job_id=job.id,
                sequence_no=1,
                phase="import",
                action_type="library_file_registered",
                status=ImportJobActionStatus.COMPLETED,
                payload={"storage_mode": "managed", "transfer_method": "copy"},
            ),
            ImportJobAction(
                import_job_id=job.id,
                sequence_no=2,
                phase="import",
                action_type="library_file_registered",
                status=ImportJobActionStatus.COMPLETED,
                payload={"storage_mode": "referenced", "transfer_method": "leave_in_place"},
            ),
            # Legacy journals did not persist storage_mode. Transfer method remains
            # enough to classify their ownership without loading every payload.
            ImportJobAction(
                import_job_id=job.id,
                sequence_no=3,
                phase="import",
                action_type="library_file_registered",
                status=ImportJobActionStatus.COMPLETED,
                payload={"transfer_method": "copy"},
            ),
            ImportJobAction(
                import_job_id=job.id,
                sequence_no=4,
                phase="import",
                action_type="library_file_registered",
                status=ImportJobActionStatus.ROLLBACK_FAILED,
                payload={"storage_mode": "managed", "transfer_method": "copy"},
                error_message="Destination changed after import.",
            ),
        ]
    )
    await db_session.flush()

    context = await load_import_results_context(db_session, job)

    assert context["managed_artifacts_created"] == 2
    assert context["referenced_files_registered"] == 1
    assert context["rollback_managed_candidates"] == 2
    assert context["rollback_reference_candidates"] == 1
    assert context["rollback_manual_recovery_count"] == 1


@pytest.mark.asyncio
async def test_load_import_results_context_exposes_incomplete_rollback_truth(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    from pullbox.ui.import_results_context import load_import_results_context

    job = ImportJob(
        source_path="/tmp/comics",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.FAILED,
        import_started_at=datetime.now(UTC),
        error_message="Rollback incomplete: 1 action requires manual recovery.",
        progress_snapshot={
            "mode": "rollback",
            "phase": "rollback_incomplete",
            "progress": 50,
            "rollback_action_count": 2,
            "rollback_actions_rolled_back": 1,
            "rollback_manual_recovery_count": 1,
        },
    )
    db_session.add(job)
    await db_session.flush()
    db_session.add_all(
        [
            ImportJobAction(
                import_job_id=job.id,
                sequence_no=1,
                phase="import",
                action_type="series_created",
                status=ImportJobActionStatus.ROLLED_BACK,
            ),
            ImportJobAction(
                import_job_id=job.id,
                sequence_no=2,
                phase="import",
                action_type="library_file_registered",
                status=ImportJobActionStatus.ROLLBACK_FAILED,
                error_message="Destination changed after import.",
            ),
        ]
    )
    await db_session.flush()

    context = await load_import_results_context(db_session, job)

    assert context["can_rollback"] is False
    assert context["rollback_incomplete"] is True
    assert context["rollback_action_count"] == 2
    assert context["rollback_actions_rolled_back"] == 1
    assert context["rollback_manual_recovery_count"] == 1


async def _add_story_arc_ownership_journal(
    db_session,  # type: ignore[no-untyped-def]
    job: ImportJob,
    *,
    sequence_start: int,
    include_managed: bool = True,
) -> None:
    root = LibraryRoot(
        name=f"Story Arc results {job.id}",
        path=f"/library/story-arc-results-{job.id}",
        enabled=True,
    )
    series = Series(
        title=f"Story Arc results {job.id}",
        sort_title=f"story arc results {job.id}",
        library_root=root,
    )
    issue = Issue(series=series, issue_number=1, issue_number_text="1")
    library_file = LibraryFile(
        file_path=f"{root.path}/issue.cbz",
        file_name="issue.cbz",
        file_size=123,
        file_format=FileFormat.CBZ,
        file_modified_at=datetime.now(UTC),
        match_confidence=MatchConfidence.HIGH,
        issue=issue,
        library_root=root,
    )
    arc = StoryArc(name=f"Results Arc {job.id}", source_kind=StoryArcSourceKind.MYLAR3)
    membership = IssueStoryArc(
        story_arc=arc,
        issue=issue,
        sequence_number=1,
        source_ordinal=1,
        resolution_state=StoryArcResolutionState.RESOLVED,
        source_kind=StoryArcSourceKind.MYLAR3,
    )
    staged_arc = ImportedStoryArc(
        import_job=job,
        source_kind=StoryArcSourceKind.MYLAR3,
        source_key=f"mylar3:results:{job.id}",
        source_ordinal=1,
        name=arc.name,
        status=ImportedStoryArcStatus.IMPORTED,
        materialized_story_arc=arc,
    )
    staged_entry = ImportedStoryArcEntry(
        imported_story_arc=staged_arc,
        matched_issue=issue,
        materialized_membership=membership,
        source_ordinal=1,
        reading_order=1,
        resolution_state=StoryArcResolutionState.RESOLVED,
        source_kind=StoryArcSourceKind.MYLAR3,
    )
    db_session.add_all([library_file, membership, staged_entry])
    await db_session.flush()

    reference_action = ImportJobAction(
        import_job_id=job.id,
        sequence_no=sequence_start,
        phase="story_arcs",
        action_type="story_arc_referenced_placement_attached",
        status=ImportJobActionStatus.COMPLETED,
        payload={},
    )
    db_session.add(reference_action)
    await db_session.flush()
    reference_placement = StoryArcPlacement(
        issue_story_arc_id=membership.id,
        library_file_id=library_file.id,
        placement_path=f"/imports/story-arc-reference-{job.id}.cbz",
        mode=StoryArcPlacementMode.REFERENCE_ONLY,
        ownership=StoryArcPlacementOwnership.REFERENCED,
        source_kind=StoryArcSourceKind.MYLAR3,
        source_import_job_id=job.id,
        creating_action_id=reference_action.id,
        rendered_reading_order=1,
        state=StoryArcPlacementState.CURRENT,
        last_result={"schema_version": 1, "code": "reference_current"},
    )
    db_session.add(reference_placement)
    await db_session.flush()
    reference_action.payload = {
        "schema_version": 1,
        "journal_state": "completed",
        "placement_id": reference_placement.id,
        "issue_story_arc_id": membership.id,
        "imported_story_arc_entry_id": staged_entry.id,
        "placement_path": reference_placement.placement_path,
        "source_kind": StoryArcSourceKind.MYLAR3.value,
        "source_import_job_id": job.id,
        "expected_after": {"state": "current"},
    }

    async def add_managed(state: StoryArcSyncWorkState, ordinal: int) -> None:
        action = ImportJobAction(
            import_job_id=job.id,
            sequence_no=sequence_start + ordinal,
            phase="story_arc_placements",
            action_type="story_arc_managed_placement_requested",
            status=ImportJobActionStatus.COMPLETED,
            payload={},
        )
        db_session.add(action)
        await db_session.flush()
        generation = f"{ordinal:064x}"
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
            source_file_size=library_file.file_size,
            source_file_modified_at=library_file.file_modified_at,
            story_arc_revision=1,
            membership_sequence=1,
            policy_schema_version=1,
            state=state,
        )
        db_session.add(work)
        await db_session.flush()
        action.payload = {
            "schema_version": 1,
            "sync_work_id": work.id,
            "membership_id": membership.id,
            "desired_generation": generation,
            "imported_story_arc_id": staged_arc.id,
            "imported_story_arc_entry_id": staged_entry.id,
            "source_import_job_id": job.id,
        }
        if state is StoryArcSyncWorkState.COMPLETED:
            db_session.add(
                StoryArcPlacement(
                    issue_story_arc_id=membership.id,
                    library_file_id=library_file.id,
                    library_root_id=root.id,
                    placement_path=f"{root.path}/arc-{ordinal}.cbz",
                    mode=StoryArcPlacementMode.COPY,
                    ownership=StoryArcPlacementOwnership.MANAGED,
                    source_kind=StoryArcSourceKind.PULLBOX,
                    source_import_job_id=job.id,
                    creating_action_id=action.id,
                    rendered_reading_order=1,
                    policy_schema_version=1,
                    state=StoryArcPlacementState.CURRENT,
                    last_result={"schema_version": 1, "status": "complete"},
                )
            )

    if include_managed:
        await add_managed(StoryArcSyncWorkState.COMPLETED, 1)
        await add_managed(StoryArcSyncWorkState.QUEUED, 2)
    await db_session.flush()


@pytest.mark.asyncio
async def test_reference_only_arc_results_follow_entry_to_its_staged_arc(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    from pullbox.ui.import_results_context import load_import_results_context

    job = ImportJob(
        source_path="/imports/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.COMPLETED,
    )
    db_session.add(job)
    await db_session.flush()
    await _add_story_arc_ownership_journal(
        db_session,
        job,
        sequence_start=1,
        include_managed=False,
    )

    context = await load_import_results_context(db_session, job)

    assert context["managed_artifacts_created"] == 0
    assert context["referenced_files_registered"] == 1
    assert context["rollback_managed_candidates"] == 0
    assert context["rollback_reference_candidates"] == 1


@pytest.mark.asyncio
async def test_arc_only_results_count_only_completed_verified_story_arc_placements(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    from pullbox.ui.import_results_context import load_import_results_context

    job = ImportJob(
        source_path="/imports/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.COMPLETED,
    )
    db_session.add(job)
    await db_session.flush()
    await _add_story_arc_ownership_journal(db_session, job, sequence_start=1)

    context = await load_import_results_context(db_session, job)

    assert context["managed_artifacts_created"] == 1
    assert context["referenced_files_registered"] == 1
    assert context["rollback_managed_candidates"] == 1
    assert context["rollback_reference_candidates"] == 1


@pytest.mark.asyncio
async def test_mixed_results_add_story_arc_ownership_to_normal_file_actions(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    from pullbox.ui.import_results_context import load_import_results_context

    job = ImportJob(
        source_path="/imports",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.COMPLETED,
    )
    db_session.add(job)
    await db_session.flush()
    db_session.add_all(
        [
            ImportJobAction(
                import_job_id=job.id,
                sequence_no=1,
                phase="import",
                action_type="library_file_registered",
                status=ImportJobActionStatus.COMPLETED,
                payload={"storage_mode": "managed", "transfer_method": "copy"},
            ),
            ImportJobAction(
                import_job_id=job.id,
                sequence_no=2,
                phase="import",
                action_type="library_file_registered",
                status=ImportJobActionStatus.COMPLETED,
                payload={
                    "storage_mode": "referenced",
                    "transfer_method": "leave_in_place",
                },
            ),
        ]
    )
    await db_session.flush()
    await _add_story_arc_ownership_journal(db_session, job, sequence_start=3)

    context = await load_import_results_context(db_session, job)

    assert context["managed_artifacts_created"] == 2
    assert context["referenced_files_registered"] == 2
    assert context["rollback_managed_candidates"] == 2
    assert context["rollback_reference_candidates"] == 2


@pytest.mark.asyncio
async def test_completed_results_group_large_safety_backlog_without_loading_every_row(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    from pullbox.ui.import_results_context import load_import_results_context

    job = ImportJob(
        source_path="/imports/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.COMPLETED,
    )
    db_session.add(job)
    await db_session.flush()
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Large library",
        status=ImportSeriesStatus.IMPORTED,
    )
    db_session.add(imported_series)
    await db_session.flush()

    rows = []
    for index in range(12):
        category = (
            ImportSafetyCategory.SOURCE_MISSING
            if index < 10
            else ImportSafetyCategory.DANGEROUS_PATH_OR_PAYLOAD
        )
        block = build_import_safety_diagnostics(
            category.value,
            code=category.value,
        )
        rows.append(
            ImportedFile(
                import_job_id=job.id,
                import_series_id=imported_series.id,
                file_path=f"/imports/file-{index}.cbz",
                file_name=f"file-{index}.cbz",
                file_size=1,
                file_format="cbz",
                status=ImportedFileStatus.SAFETY_BLOCKED,
                diagnostics={"safety_block": block},
            )
        )
    db_session.add_all(rows)
    await db_session.flush()

    context = await load_import_results_context(db_session, job)

    assert context["files_safety_blocked"] == 12
    assert context["safety_blocked_files"] == []
    summaries = {item["category"]: item for item in context["safety_category_summaries"]}
    assert summaries["source_missing"]["count"] == 10
    assert len(summaries["source_missing"]["examples"]) == 3
    assert summaries["source_missing"]["bucket"] == "safe_action"
    assert summaries["dangerous_path_or_payload"]["bucket"] == "needs_review"
    assert context["cleanup_safe_action_count"] == 10
    assert context["cleanup_needs_review_count"] == 2
    assert context["cleanup_action_summaries"][0]["action"] == "dismiss_missing_references"
    assert context["cleanup_action_summaries"][0]["affected_file_count"] == 10


@pytest.mark.asyncio
async def test_failed_results_keep_a_bounded_safety_exception_list(db_session) -> None:  # type: ignore[no-untyped-def]
    from pullbox.ui.import_results_context import load_import_results_context

    job = ImportJob(
        source_path="/imports/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.FAILED,
    )
    db_session.add(job)
    await db_session.flush()
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Interrupted library",
        status=ImportSeriesStatus.FAILED,
    )
    db_session.add(imported_series)
    await db_session.flush()
    block = build_import_safety_diagnostics(
        ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT.value,
        code=ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT.value,
        overrideable_hint=True,
    )
    db_session.add_all(
        [
            ImportedFile(
                import_job_id=job.id,
                import_series_id=imported_series.id,
                file_path=f"/imports/large-{index:03}.cbz",
                file_name=f"large-{index:03}.cbz",
                file_size=1,
                file_format="cbz",
                status=ImportedFileStatus.SAFETY_BLOCKED,
                diagnostics={"safety_block": block},
            )
            for index in range(105)
        ]
    )
    await db_session.flush()

    context = await load_import_results_context(db_session, job)

    assert context["files_safety_blocked"] == 105
    assert len(context["safety_blocked_files"]) == 100
    assert context["safety_blocked_files_truncated"] == 5
