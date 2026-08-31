"""Bounded, aggregate-only import and Story Arc diagnostic contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _assert_no_forbidden_diagnostic_keys(value: object) -> None:
    forbidden = {
        "archive_metadata",
        "error_detail",
        "error_message",
        "file_path",
        "library_path",
        "payload",
        "placement_path",
        "source_file_path",
        "source_location",
        "source_path",
    }
    if isinstance(value, dict):
        assert forbidden.isdisjoint(value)
        for child in value.values():
            _assert_no_forbidden_diagnostic_keys(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_forbidden_diagnostic_keys(child)


@pytest.mark.asyncio
async def test_collect_import_story_arc_diagnostics_is_aggregate_only_and_sanitized(
    db_session: AsyncSession,
) -> None:
    from pullbox.models.import_job import (
        ImportedFile,
        ImportedFileStatus,
        ImportedSeries,
        ImportFileHandlingMode,
        ImportJob,
        ImportJobAction,
        ImportJobActionStatus,
        ImportJobLog,
        ImportJobStatus,
        ImportSeriesStatus,
        ImportSourceType,
    )
    from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
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
    from pullbox.models.story_arc_sync import (
        StoryArcSyncReason,
        StoryArcSyncWork,
        StoryArcSyncWorkState,
    )
    from pullbox.services.diagnostic_import_collectors import (
        collect_import_story_arc_diagnostics,
    )

    started_at = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    completed = ImportJob(
        source_path="/private/comics/never-emit",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.COMPLETED,
        file_handling_mode=ImportFileHandlingMode.IN_PLACE,
        effective_import_strategy="in_place",
        effective_transfer_method="none",
        source_preserved=True,
        story_arc_import_requested=True,
        story_arc_materialization_requested=False,
        scan_started_at=started_at,
        scan_completed_at=started_at + timedelta(seconds=2),
        match_started_at=started_at + timedelta(seconds=2),
        match_completed_at=started_at + timedelta(seconds=5),
        import_started_at=started_at + timedelta(seconds=6),
        import_completed_at=started_at + timedelta(seconds=10),
    )
    cancelled = ImportJob(
        source_path="/private/mylar/never-emit.db",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.CANCELLED,
        file_handling_mode=ImportFileHandlingMode.MANAGED_COPY,
        effective_import_strategy="managed_copy",
        effective_transfer_method="copy",
        story_arc_placement_followup_pending=True,
    )
    db_session.add_all([completed, cancelled])
    await db_session.flush()

    db_session.add_all(
        [
            ImportJobLog(
                import_job_id=cancelled.id,
                logged_at=started_at,
                level="INFO",
                event="import_cancel_requested",
                message="private /source/path must not survive",
                data={"payload": {"source_path": "/private/source/path"}},
            ),
            ImportJobLog(
                import_job_id=cancelled.id,
                logged_at=started_at + timedelta(milliseconds=1250),
                level="INFO",
                event="import_scan_cancelled",
                message="cancelled",
                data={},
            ),
            ImportJobLog(
                import_job_id=completed.id,
                logged_at=started_at + timedelta(seconds=5),
                level="INFO",
                event="import_step2_timing",
                message="timing",
                data={
                    "scan_duration_ms": 2000,
                    "analyze_duration_ms": 500,
                    "series_matching_duration_ms": 3000,
                    "file_matching_duration_ms": 1500,
                    "total_duration_ms": 7000,
                    "archive_metadata": {"private_member": "secret.cbz"},
                },
            ),
            ImportJobLog(
                import_job_id=cancelled.id,
                logged_at=started_at + timedelta(seconds=2),
                level="ERROR",
                event="import_scan_failed",
                message="password=hunter2 at /private/source/path",
                data={"error": "free-form details"},
            ),
        ]
    )
    db_session.add_all(
        [
            ImportJobAction(
                import_job_id=completed.id,
                sequence_no=1,
                phase="library_file",
                action_type="create_library_file",
                status=ImportJobActionStatus.COMPLETED,
                payload={"file_path": "/private/library/never-emit.cbz"},
            ),
            ImportJobAction(
                import_job_id=cancelled.id,
                sequence_no=2,
                phase="/private/invalid-phase",
                action_type="/private/invalid-action",
                status=ImportJobActionStatus.ROLLBACK_FAILED,
                error_message="secret rollback detail /private/library",
                payload={"archive_metadata": "secret"},
            ),
        ]
    )

    imported_series = ImportedSeries(
        import_job_id=cancelled.id,
        status=ImportSeriesStatus.RECOVERY_PENDING,
        raw_series_name="Private Series Name",
        file_count=1,
        sample_paths=["/private/sample/never-emit.cbz"],
        source_folder="/private/sample",
    )
    db_session.add(imported_series)
    await db_session.flush()
    db_session.add(
        ImportedFile(
            import_job_id=cancelled.id,
            import_series_id=imported_series.id,
            file_path="/private/sample/never-emit.cbz",
            file_name="never-emit.cbz",
            file_size=123,
            file_format="cbz",
            status=ImportedFileStatus.SAFETY_BLOCKED,
            error_message="PermissionError: /private/sample/never-emit.cbz",
            diagnostics={
                "safety_block": {
                    "kind": "file_safety_blocked",
                    "category": "permission_unreadable",
                    "code": "permission_denied",
                    "reason": "PermissionError: /private/sample/never-emit.cbz",
                    "details": ["archive member secret/never-emit.jpg"],
                }
            },
        )
    )

    staged_arc = ImportedStoryArc(
        import_job_id=completed.id,
        source_kind=StoryArcSourceKind.FOLDER,
        source_key="private-source-key",
        source_ordinal=1,
        name="Private Arc Name",
        status=ImportedStoryArcStatus.NEEDS_REVIEW,
        diagnostics={"archive_metadata": {"member": "private/never-emit.jpg"}},
    )
    db_session.add(staged_arc)
    await db_session.flush()
    db_session.add(
        ImportedStoryArcEntry(
            imported_story_arc_id=staged_arc.id,
            source_ordinal=1,
            reading_order=1,
            resolution_state=StoryArcResolutionState.CONFLICT,
            source_kind=StoryArcSourceKind.FOLDER,
            source_issue_number_text="10000",
            source_series_name="Private Series Name",
            source_location="/private/story-arcs/never-emit.cbz",
            diagnostics={"error": "free-form parse failure"},
        )
    )

    canonical_arc = StoryArc(
        name="Canonical Private Arc",
        source_kind=StoryArcSourceKind.FOLDER,
        sync_enabled=True,
    )
    db_session.add(canonical_arc)
    await db_session.flush()
    membership = IssueStoryArc(
        story_arc_id=canonical_arc.id,
        sequence_number=1,
        source_ordinal=1,
        resolution_state=StoryArcResolutionState.MISSING,
        source_kind=StoryArcSourceKind.FOLDER,
        source_issue_number_text="1e+86",
    )
    db_session.add(membership)
    await db_session.flush()
    library_root = LibraryRoot(
        name="Diagnostic Comics",
        path="/private/library",
        enabled=True,
    )
    diagnostic_library_file = LibraryFile(
        file_path="/private/library/never-emit.cbz",
        file_name="never-emit.cbz",
        file_size=123,
        file_format=FileFormat.CBZ,
        file_modified_at=started_at,
        match_confidence=MatchConfidence.UNMATCHED,
        library_root=library_root,
    )
    db_session.add(diagnostic_library_file)
    await db_session.flush()
    db_session.add(
        StoryArcPlacement(
            issue_story_arc_id=membership.id,
            placement_path="/private/story-arcs/never-emit.cbz",
            mode=StoryArcPlacementMode.REFERENCE_ONLY,
            ownership=StoryArcPlacementOwnership.REFERENCED,
            source_kind=StoryArcSourceKind.FOLDER,
            state=StoryArcPlacementState.MISSING,
            last_result={"error_detail": "/private/story-arcs/never-emit.cbz"},
        )
    )
    db_session.add(
        StoryArcSyncWork(
            issue_story_arc_id=membership.id,
            library_file_id=diagnostic_library_file.id,
            origin_import_job_id=cancelled.id,
            desired_generation="generation",
            source_signature_hash="hash",
            source_file_path="/private/library/never-emit.cbz",
            source_file_size=123,
            source_file_modified_at=started_at,
            story_arc_revision=1,
            membership_sequence=1,
            policy_schema_version=1,
            reason=StoryArcSyncReason.DISCREPANCY_RECOVERY,
            state=StoryArcSyncWorkState.FAILED,
            attempt_count=3,
            last_error_category="filesystem",
            last_error_code="/private/free-form-error",
            last_error_detail="password=hunter2 /private/library/never-emit.cbz",
        )
    )
    await db_session.flush()

    diagnostics = await collect_import_story_arc_diagnostics(db_session)

    assert diagnostics["schema_version"] == 1
    assert diagnostics["imports"]["jobs"]["total"] == 2
    assert diagnostics["imports"]["jobs"]["by_status"] == {
        "cancelled": 1,
        "completed": 1,
    }
    assert diagnostics["imports"]["safety"]["categories"][0]["category"] == (
        "permission_unreadable"
    )
    assert diagnostics["imports"]["safety"]["categories"][0]["count"] == 1
    assert diagnostics["imports"]["safety"]["categories"][0]["examples"] == []
    assert diagnostics["story_arcs"]["canonical"]["total"] == 1
    assert diagnostics["story_arcs"]["placements"]["by_state"] == {"missing": 1}
    assert diagnostics["story_arcs"]["failed_issue_numbers"]["items"] == [
        {"count": 1, "issue_number": "10000", "resolution_state": "conflict"},
        {"count": 1, "issue_number": "1e+86", "resolution_state": "missing"},
    ]
    assert diagnostics["performance"]["stage_duration_ms"]["scan"]["average"] == 2000
    assert diagnostics["performance"]["recorded_step2_duration_ms"]["analyze"]["maximum"] == 500
    assert diagnostics["performance"]["cancellation_latency_ms"]["maximum"] == 1250
    assert diagnostics["recovery"]["action_journal"]["by_status"] == {
        "completed": 1,
        "rollback_failed": 1,
    }
    assert diagnostics["recovery"]["story_arc_sync_work"]["by_state"] == {"failed": 1}
    assert diagnostics["recovery"]["story_arc_sync_work"]["failure_codes"] == {"other": 1}
    assert diagnostics["recovery"]["series_pending_recovery"] == 1
    assert "process_peak_rss_bytes" in diagnostics["performance"]["resource_snapshot"]

    _assert_no_forbidden_diagnostic_keys(diagnostics)
    serialized = json.dumps(diagnostics, sort_keys=True)
    for secret in (
        "/private/",
        "hunter2",
        "Private Arc Name",
        "Private Series Name",
        "never-emit",
        "private_member",
        "secret.cbz",
    ):
        assert secret not in serialized


@pytest.mark.asyncio
async def test_collect_import_story_arc_diagnostics_bounds_sampled_rows(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pullbox.models.import_job import (
        ImportedFile,
        ImportedFileStatus,
        ImportedSeries,
        ImportJob,
        ImportJobStatus,
        ImportSeriesStatus,
        ImportSourceType,
    )
    from pullbox.models.story_arc import (
        ImportedStoryArcStatus,
        StoryArcResolutionState,
        StoryArcSourceKind,
    )
    from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
    from pullbox.services import diagnostic_import_collectors as collector

    monkeypatch.setattr(collector, "MAX_SAFETY_ROWS", 2)
    monkeypatch.setattr(collector, "MAX_FAILED_ISSUE_NUMBER_ROWS", 2)

    job = ImportJob(
        source_path="/private/source",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
    )
    db_session.add(job)
    await db_session.flush()
    series = ImportedSeries(
        import_job_id=job.id,
        status=ImportSeriesStatus.MATCHED,
        raw_series_name="Private Series",
        file_count=3,
    )
    arc = ImportedStoryArc(
        import_job_id=job.id,
        source_kind=StoryArcSourceKind.FOLDER,
        source_key="arc",
        source_ordinal=1,
        status=ImportedStoryArcStatus.NEEDS_REVIEW,
    )
    db_session.add_all([series, arc])
    await db_session.flush()
    for ordinal in range(3):
        db_session.add(
            ImportedFile(
                import_job_id=job.id,
                import_series_id=series.id,
                file_path=f"/private/{ordinal}.cbz",
                file_name=f"{ordinal}.cbz",
                file_size=1,
                file_format="cbz",
                status=ImportedFileStatus.SAFETY_BLOCKED,
                diagnostics={
                    "safety_block": {
                        "category": "permission_unreadable",
                        "code": "permission_denied",
                        "reason": "permission denied",
                    }
                },
            )
        )
        db_session.add(
            ImportedStoryArcEntry(
                imported_story_arc_id=arc.id,
                source_ordinal=ordinal,
                resolution_state=StoryArcResolutionState.MISSING,
                source_kind=StoryArcSourceKind.FOLDER,
                source_issue_number_text=str(ordinal + 1),
            )
        )
    await db_session.flush()

    diagnostics = await collector.collect_import_story_arc_diagnostics(db_session)

    safety = diagnostics["imports"]["safety"]
    assert safety["rows_sampled"] == 2
    assert safety["sample_truncated"] is True
    issue_numbers = diagnostics["story_arcs"]["failed_issue_numbers"]
    assert len(issue_numbers["items"]) == 2
    assert issue_numbers["sample_truncated"] is True
