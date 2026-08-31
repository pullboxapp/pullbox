"""Step 4 materializes arcs without archive-content reads or source mutations."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import pullbox.services.import_job_execution as execution_module
from pullbox.core.exceptions import JobCancelledError
from pullbox.models import Base
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobAction,
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
    StoryArcPlacement,
    StoryArcPlacementMode,
    StoryArcPlacementOwnership,
    StoryArcPlacementState,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.models.story_arc_sync import StoryArcSyncWork
from pullbox.services import import_story_arc_materialization as materialization_module
from pullbox.services.import_job_actions import record_action, record_actions
from pullbox.services.import_story_arc_materialization import (
    materialize_confirmed_story_arcs,
)
from pullbox.services.import_workflow_state import raise_if_job_cancelled

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


async def _add_library_file(
    session: AsyncSession,
    *,
    issue: Issue,
    root: LibraryRoot,
    path: Path,
) -> LibraryFile:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"canonical-story-arc-issue")
    stat_result = path.stat()
    library_file = LibraryFile(
        file_path=str(path),
        file_name=path.name,
        file_size=stat_result.st_size,
        file_format=FileFormat.CBZ,
        file_modified_at=datetime.now(UTC),
        match_confidence=MatchConfidence.HIGH,
        issue_id=issue.id,
        library_root_id=root.id,
        source_signature={
            "schema_version": 1,
            "resolved_path": str(path.resolve()),
            "size": stat_result.st_size,
            "mtime_ns": stat_result.st_mtime_ns,
            "device": stat_result.st_dev,
            "inode": stat_result.st_ino,
        },
    )
    session.add(library_file)
    await session.flush()
    return library_file


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
    symlink_style: str | None = None,
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
            "symlink_style": symlink_style,
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
        "story_arc_managed_placement_canonical_file_missing",
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
        "managed_placements_queued": 0,
        "managed_placements_reused": 0,
    }
    assert await db_session.scalar(select(func.count()).select_from(StoryArcPlacement)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "symlink_style"),
    [
        ("copy", None),
        ("hardlink", None),
        ("symlink", "absolute"),
        ("symlink", "relative"),
    ],
)
async def test_confirmed_managed_policy_queues_one_import_owned_command_when_future_sync_is_off(
    db_session: AsyncSession,
    tmp_path: Path,
    mode: str,
    symlink_style: str | None,
) -> None:
    canonical_root_path = tmp_path / "library"
    canonical_root_path.mkdir()
    destination = canonical_root_path / "Story Arcs"
    destination.mkdir()
    root = LibraryRoot(name="Comics", path=str(canonical_root_path), enabled=True)
    db_session.add(root)
    await db_session.flush()
    issue = await _add_issue(
        db_session,
        series_name="Queued Series",
        issue_number=1.0,
        issue_number_text="1",
    )
    library_file = await _add_library_file(
        db_session,
        issue=issue,
        root=root,
        path=canonical_root_path / "Queued Series 001.cbz",
    )
    job = await _add_job(db_session)
    staged = await _add_staged_arc(
        db_session,
        job=job,
        name=f"Queued {mode}",
        source_key=f"mylar3:queued:{mode}:{symlink_style}",
        source_arc_id=f"queued-{mode}-{symlink_style}",
        policy=_confirmed_policy(
            source="mylar3",
            mode=mode,
            target_library_root_id=root.id,
            destination_root=str(destination),
            symlink_style=symlink_style,
            synchronize=False,
        ),
    )
    entry = await _add_staged_entry(
        db_session,
        staged_arc=staged,
        source_ordinal=1,
        reading_order=1,
        issue_number_text="1",
        matched_issue_id=issue.id,
    )

    first = await materialize_confirmed_story_arcs(
        db_session,
        import_job_id=job.id,
        record_action=record_action,
    )

    work = (await db_session.scalars(select(StoryArcSyncWork))).one()
    action = (
        await db_session.scalars(
            select(ImportJobAction).where(
                ImportJobAction.action_type == "story_arc_managed_placement_requested"
            )
        )
    ).one()
    membership = await db_session.get(IssueStoryArc, entry.materialized_membership_id)
    assert membership is not None
    assert first.managed_placements_queued == 1
    assert first.managed_placements_reused == 0
    assert membership.sync_eligible is False
    assert work.library_file_id == library_file.id
    assert work.issue_story_arc_id == membership.id
    assert work.origin_import_action_id == action.id
    assert set(action.payload) == {
        "schema_version",
        "sync_work_id",
        "membership_id",
        "desired_generation",
        "imported_story_arc_id",
        "imported_story_arc_entry_id",
        "source_import_job_id",
    }
    assert action.payload == {
        "schema_version": 1,
        "sync_work_id": work.id,
        "membership_id": membership.id,
        "desired_generation": work.desired_generation,
        "imported_story_arc_id": staged.id,
        "imported_story_arc_entry_id": entry.id,
        "source_import_job_id": job.id,
    }

    staged.status = ImportedStoryArcStatus.CONFIRMED
    await db_session.flush()
    retry = await materialize_confirmed_story_arcs(
        db_session,
        import_job_id=job.id,
        record_action=record_action,
    )

    assert retry.managed_placements_queued == 0
    assert retry.managed_placements_reused == 0
    assert await db_session.scalar(select(func.count()).select_from(StoryArcSyncWork)) == 1
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ImportJobAction)
            .where(ImportJobAction.action_type == "story_arc_managed_placement_requested")
        )
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("durable_page_boundary", [False, True])
async def test_duplicate_staged_entries_share_one_import_owned_managed_placement(
    db_session: AsyncSession,
    tmp_path: Path,
    durable_page_boundary: bool,
) -> None:
    canonical_root_path = tmp_path / "library"
    canonical_root_path.mkdir()
    destination = canonical_root_path / "Story Arcs"
    destination.mkdir()
    root = LibraryRoot(name="Comics", path=str(canonical_root_path), enabled=True)
    db_session.add(root)
    await db_session.flush()
    issue = await _add_issue(
        db_session,
        series_name="Duplicate Series",
        issue_number=1.0,
        issue_number_text="1",
    )
    await _add_library_file(
        db_session,
        issue=issue,
        root=root,
        path=canonical_root_path / "Duplicate Series 001.cbz",
    )
    job = await _add_job(db_session)
    staged = await _add_staged_arc(
        db_session,
        job=job,
        name="Duplicate Managed Arc",
        source_key=f"mylar3:duplicate-managed:{durable_page_boundary}",
        source_arc_id=f"duplicate-managed-{durable_page_boundary}",
        policy=_confirmed_policy(
            source="mylar3",
            mode="copy",
            target_library_root_id=root.id,
            destination_root=str(destination),
        ),
    )
    first = await _add_staged_entry(
        db_session,
        staged_arc=staged,
        source_ordinal=1,
        reading_order=1,
        issue_number_text="1",
        matched_issue_id=issue.id,
    )
    duplicate = await _add_staged_entry(
        db_session,
        staged_arc=staged,
        source_ordinal=2,
        reading_order=2,
        issue_number_text="1",
        matched_issue_id=issue.id,
    )

    async def commit_checkpoint() -> None:
        await db_session.commit()

    result = await materialize_confirmed_story_arcs(
        db_session,
        import_job_id=job.id,
        entry_checkpoint_size=1 if durable_page_boundary else 100,
        durable_checkpoint=commit_checkpoint if durable_page_boundary else None,
        record_actions=record_actions,
    )

    work = (await db_session.scalars(select(StoryArcSyncWork))).one()
    managed_action = (
        await db_session.scalars(
            select(ImportJobAction).where(
                ImportJobAction.action_type == "story_arc_managed_placement_requested"
            )
        )
    ).one()
    assert result.memberships_created == 1
    assert result.memberships_reused == 1
    assert result.managed_placements_queued == 1
    assert result.managed_placements_reused == 0
    assert first.materialized_membership_id == duplicate.materialized_membership_id
    assert work.issue_story_arc_id == first.materialized_membership_id
    assert work.origin_imported_story_arc_entry_id == first.id
    assert managed_action.payload["imported_story_arc_entry_id"] == first.id
    assert (
        "duplicate_issue_membership_reused"
        in duplicate.diagnostics["materialization"]["warning_codes"]
    )
    assert staged.diagnostics["materialization"]["counts"]["managed_placements_queued"] == 1

    staged.status = ImportedStoryArcStatus.CONFIRMED
    await db_session.flush()
    replay = await materialize_confirmed_story_arcs(
        db_session,
        import_job_id=job.id,
        entry_checkpoint_size=1 if durable_page_boundary else 100,
        durable_checkpoint=commit_checkpoint if durable_page_boundary else None,
        record_actions=record_actions,
    )

    assert replay.managed_placements_queued == 0
    assert replay.managed_placements_reused == 0
    assert int(await db_session.scalar(select(func.count(StoryArcSyncWork.id))) or 0) == 1
    assert (
        int(
            await db_session.scalar(
                select(func.count(ImportJobAction.id)).where(
                    ImportJobAction.action_type == "story_arc_managed_placement_requested"
                )
            )
            or 0
        )
        == 1
    )


@pytest.mark.asyncio
async def test_managed_placement_handoff_chunks_a_large_entry_page_at_200(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = ImportJob(
        id=7,
        source_path="/private/source",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.IMPORTING,
        progress_snapshot={"phase": "story_arcs"},
    )
    staged_arc = ImportedStoryArc(
        id=11,
        import_job_id=job.id,
        source_kind=StoryArcSourceKind.MYLAR3,
        source_key="mylar3:chunked",
        source_ordinal=1,
        name="Chunked Arc",
    )
    arc = StoryArc(id=13, name="Chunked Arc")
    context = materialization_module._ArcMaterializationContext(
        staged_arc=staged_arc,
        arc=arc,
        policy=materialization_module._ValidatedPolicy(
            activated=True,
            snapshot={"mode": "copy"},
        ),
        counts=materialization_module._MutableCounts(),
    )
    requests = [
        materialization_module._ManagedPlacementRequest(
            proposal=materialization_module.ImportStoryArcSyncProposal(
                library_file=LibraryFile(id=ordinal, issue_id=ordinal),
                membership=IssueStoryArc(
                    id=ordinal,
                    story_arc_id=arc.id,
                    issue_id=ordinal,
                ),
                story_arc=arc,
                imported_story_arc_id=int(staged_arc.id),
                imported_story_arc_entry_id=ordinal,
            ),
            context=context,
        )
        for ordinal in range(1, 251)
    ]
    chunk_sizes: list[int] = []

    async def fake_enqueue_batch(*_args: object, **kwargs: object) -> list[object]:
        proposals = kwargs["proposals"]
        chunk_sizes.append(len(proposals))
        return [
            materialization_module.StoryArcImportSyncEnqueueResult(
                work=None,
                action=None,
                classification="created",
                desired_generation=str(index),
            )
            for index, _proposal in enumerate(proposals)
        ]

    async def unused_record_actions(*_args: object, **_kwargs: object) -> list[ImportJobAction]:
        raise AssertionError("The fake batch enqueue should not invoke the journal callback")

    monkeypatch.setattr(
        materialization_module,
        "enqueue_import_story_arc_sync_work_batch",
        fake_enqueue_batch,
    )
    counts = materialization_module._MutableCounts()
    durable_checkpoint = AsyncMock()

    await materialization_module._enqueue_managed_placement_requests(
        db_session,
        job=job,
        requests=requests,
        counts=counts,
        record_actions=unused_record_actions,
        cancellation_check=None,
        durable_checkpoint=durable_checkpoint,
    )

    assert chunk_sizes == [200, 50]
    assert durable_checkpoint.await_count == 2
    assert counts.managed_placements_queued == 250
    assert context.counts.managed_placements_queued == 250
    assert job.progress_snapshot["phase"] == "story_arcs"


@pytest.mark.asyncio
async def test_managed_placement_handoff_counts_only_created_and_reusable_results(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = ImportJob(
        id=7,
        source_path="/private/source",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.IMPORTING,
        progress_snapshot={"phase": "story_arcs"},
    )
    staged_arc = ImportedStoryArc(
        id=11,
        import_job_id=job.id,
        source_kind=StoryArcSourceKind.MYLAR3,
        source_key="mylar3:classifications",
        source_ordinal=1,
        name="Classification Arc",
    )
    arc = StoryArc(id=13, name="Classification Arc")
    context = materialization_module._ArcMaterializationContext(
        staged_arc=staged_arc,
        arc=arc,
        policy=materialization_module._ValidatedPolicy(
            activated=True,
            snapshot={"mode": "copy"},
        ),
        counts=materialization_module._MutableCounts(),
    )
    classifications = [
        "created",
        "in_call_duplicate",
        "existing_import_work_pending",
        "existing_import_work_completed",
        "existing_non_origin_placement",
        "existing_managed_placement",
        "existing_referenced_placement",
    ]
    requests = [
        materialization_module._ManagedPlacementRequest(
            proposal=materialization_module.ImportStoryArcSyncProposal(
                library_file=LibraryFile(id=ordinal, issue_id=ordinal),
                membership=IssueStoryArc(
                    id=ordinal,
                    story_arc_id=arc.id,
                    issue_id=ordinal,
                ),
                story_arc=arc,
                imported_story_arc_id=int(staged_arc.id),
                imported_story_arc_entry_id=ordinal,
            ),
            context=context,
        )
        for ordinal in range(1, len(classifications) + 1)
    ]

    async def fake_enqueue_batch(*_args: object, **kwargs: object) -> list[object]:
        proposals = kwargs["proposals"]
        assert len(proposals) == len(classifications)
        return [
            materialization_module.StoryArcImportSyncEnqueueResult(
                work=None,
                action=None,
                classification=classification,
                desired_generation=str(index),
            )
            for index, classification in enumerate(classifications)
        ]

    async def unused_record_actions(*_args: object, **_kwargs: object) -> list[ImportJobAction]:
        raise AssertionError("The fake batch enqueue should not invoke the journal callback")

    monkeypatch.setattr(
        materialization_module,
        "enqueue_import_story_arc_sync_work_batch",
        fake_enqueue_batch,
    )
    counts = materialization_module._MutableCounts()

    await materialization_module._enqueue_managed_placement_requests(
        db_session,
        job=job,
        requests=requests,
        counts=counts,
        record_actions=unused_record_actions,
        cancellation_check=None,
    )

    assert counts.managed_placements_queued == 1
    assert counts.managed_placements_reused == 4
    assert context.counts.managed_placements_queued == 1
    assert context.counts.managed_placements_reused == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["cancel", "crash"])
async def test_file_sqlite_interruption_between_pages_is_exact_and_restart_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: str,
) -> None:
    """A durable page remains exact after cross-session cancellation or a crash."""

    class SyntheticWorkerCrash(BaseException):
        """Model abrupt worker loss without entering the normal failure finalizer."""

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'durable-pages.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    canonical_root_path = tmp_path / "library"
    canonical_root_path.mkdir()
    destination = canonical_root_path / "Story Arcs"
    destination.mkdir()
    async with factory() as setup_session:
        root = LibraryRoot(name="Comics", path=str(canonical_root_path), enabled=True)
        setup_session.add(root)
        await setup_session.flush()
        issues: list[Issue] = []
        for ordinal in (1, 2):
            issue = await _add_issue(
                setup_session,
                series_name=f"Durable Series {ordinal}",
                issue_number=float(ordinal),
                issue_number_text=str(ordinal),
            )
            await _add_library_file(
                setup_session,
                issue=issue,
                root=root,
                path=canonical_root_path / f"Durable Series {ordinal} {ordinal:03d}.cbz",
            )
            issues.append(issue)
        job = await _add_job(setup_session)
        staged = await _add_staged_arc(
            setup_session,
            job=job,
            name="Durable Arc",
            source_key="mylar3:durable-pages",
            source_arc_id="durable-pages",
            policy=_confirmed_policy(
                source="mylar3",
                mode="copy",
                target_library_root_id=root.id,
                destination_root=str(destination),
            ),
        )
        entries = [
            await _add_staged_entry(
                setup_session,
                staged_arc=staged,
                source_ordinal=ordinal,
                reading_order=ordinal,
                issue_number_text=str(ordinal),
                matched_issue_id=issue.id,
            )
            for ordinal, issue in enumerate(issues, start=1)
        ]
        job_id = int(job.id)
        staged_id = int(staged.id)
        entry_ids = tuple(int(entry.id) for entry in entries)
        await setup_session.commit()

    real_materialize = materialization_module.materialize_confirmed_story_arcs

    async def materialize_one_entry_page(
        session: AsyncSession,
        **kwargs: object,
    ) -> materialization_module.StoryArcMaterializationResult:
        return await real_materialize(
            session,
            entry_checkpoint_size=2,
            **kwargs,  # type: ignore[arg-type]
        )

    # Compress the production 200-row handoff boundary to one row so this
    # two-connection regression stays fast while exercising two queue chunks.
    monkeypatch.setattr(
        materialization_module,
        "MAX_IMPORT_STORY_ARC_SYNC_ENQUEUE_BATCH_SIZE",
        1,
    )
    monkeypatch.setattr(
        execution_module,
        "materialize_confirmed_story_arcs",
        materialize_one_entry_page,
    )

    async with factory() as worker_session:
        worker_job = await worker_session.get(ImportJob, job_id)
        assert worker_job is not None
        interruption_triggered = False

        async def interrupt_from_second_connection_after_page(
            current_session: AsyncSession,
            current_job_id: int,
        ) -> None:
            nonlocal interruption_triggered
            if not interruption_triggered:
                async with factory() as control_session:
                    durable_work_count = int(
                        await control_session.scalar(
                            select(func.count()).select_from(StoryArcSyncWork)
                        )
                        or 0
                    )
                    if durable_work_count == 1:
                        interruption_triggered = True
                        if interruption == "crash":
                            raise SyntheticWorkerCrash
                        controlled_job = await control_session.get(ImportJob, current_job_id)
                        assert controlled_job is not None
                        controlled_job.control_request = ImportControlRequest.CANCEL
                        await control_session.commit()
            await raise_if_job_cancelled(current_session, current_job_id)

        expected_exception = JobCancelledError if interruption == "cancel" else SyntheticWorkerCrash
        with pytest.raises(expected_exception):
            await execution_module._execute_story_arc_materialization(
                worker_session,
                worker_job,
                job_id=job_id,
                raise_if_cancelled=interrupt_from_second_connection_after_page,
                record_action=record_action,
                record_actions=record_actions,
                log_event=AsyncMock(),
                emit_progress=AsyncMock(),
                estimate_remaining_seconds=lambda *_args: None,
                progress_callback=None,
                runtime_revision_state={"value": 0},
                job_started_at=None,
            )
        await worker_session.rollback()

    assert interruption_triggered is True
    async with factory() as observer_session:
        cancelled_job = await observer_session.get(ImportJob, job_id)
        staged_after_cancel = await observer_session.get(ImportedStoryArc, staged_id)
        entries_after_cancel = [
            await observer_session.get(ImportedStoryArcEntry, entry_id) for entry_id in entry_ids
        ]
        first_work = (await observer_session.scalars(select(StoryArcSyncWork))).one()
        first_managed_action = (
            await observer_session.scalars(
                select(ImportJobAction).where(
                    ImportJobAction.action_type == "story_arc_managed_placement_requested"
                )
            )
        ).one()
        assert cancelled_job is not None
        expected_control = (
            ImportControlRequest.CANCEL if interruption == "cancel" else ImportControlRequest.NONE
        )
        assert cancelled_job.control_request is expected_control
        assert staged_after_cancel is not None
        assert staged_after_cancel.status is ImportedStoryArcStatus.CONFIRMED
        assert staged_after_cancel.materialized_story_arc_id is not None
        assert entries_after_cancel[0] is not None
        assert entries_after_cancel[0].materialized_membership_id is not None
        assert entries_after_cancel[1] is not None
        assert entries_after_cancel[1].materialized_membership_id is not None
        assert first_work.origin_import_action_id == first_managed_action.id
        assert first_managed_action.payload["sync_work_id"] == first_work.id
        first_work_id = int(first_work.id)
        first_action_id = int(first_managed_action.id)

        cancelled_job.control_request = ImportControlRequest.NONE
        await observer_session.commit()

    async with factory() as restart_session:
        restart_job = await restart_session.get(ImportJob, job_id)
        assert restart_job is not None
        await execution_module._execute_story_arc_materialization(
            restart_session,
            restart_job,
            job_id=job_id,
            raise_if_cancelled=raise_if_job_cancelled,
            record_action=record_action,
            record_actions=record_actions,
            log_event=AsyncMock(),
            emit_progress=AsyncMock(),
            estimate_remaining_seconds=lambda *_args: None,
            progress_callback=None,
            runtime_revision_state={"value": 0},
            job_started_at=None,
        )
        await restart_session.commit()

    async with factory() as final_session:
        final_staged = await final_session.get(ImportedStoryArc, staged_id)
        works = list(
            (
                await final_session.scalars(select(StoryArcSyncWork).order_by(StoryArcSyncWork.id))
            ).all()
        )
        managed_actions = list(
            (
                await final_session.scalars(
                    select(ImportJobAction)
                    .where(ImportJobAction.action_type == "story_arc_managed_placement_requested")
                    .order_by(ImportJobAction.id)
                )
            ).all()
        )
        all_action_types = list(
            (
                await final_session.scalars(
                    select(ImportJobAction.action_type).order_by(ImportJobAction.id)
                )
            ).all()
        )
        assert final_staged is not None
        assert final_staged.status is ImportedStoryArcStatus.IMPORTED
        assert len(works) == 2
        assert len(managed_actions) == 2
        assert all_action_types.count("story_arc_created") == 1
        assert all_action_types.count("story_arc_membership_created") == 2
        assert all_action_types.count("story_arc_managed_placement_requested") == 2
        assert works[0].id == first_work_id
        assert managed_actions[0].id == first_action_id
        assert {work.origin_import_action_id for work in works} == {
            action.id for action in managed_actions
        }
        assert {int(action.payload["sync_work_id"]) for action in managed_actions} == {
            int(work.id) for work in works
        }
        assert await final_session.scalar(select(func.count()).select_from(StoryArc)) == 1
        assert await final_session.scalar(select(func.count()).select_from(IssueStoryArc)) == 2

    await engine.dispose()


@pytest.mark.asyncio
async def test_materialization_fetches_only_one_library_file_per_page_issue(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    canonical_root_path = tmp_path / "library"
    canonical_root_path.mkdir()
    destination = canonical_root_path / "Story Arcs"
    destination.mkdir()
    root = LibraryRoot(name="Comics", path=str(canonical_root_path), enabled=True)
    db_session.add(root)
    await db_session.flush()
    first_issue = await _add_issue(
        db_session,
        series_name="First Series",
        issue_number=1.0,
        issue_number_text="1",
    )
    second_issue = await _add_issue(
        db_session,
        series_name="Second Series",
        issue_number=2.0,
        issue_number_text="2",
    )
    canonical_ids: set[int] = set()
    for issue, prefix in ((first_issue, "First"), (second_issue, "Second")):
        issue_file_ids: list[int] = []
        for ordinal in range(3):
            library_file = await _add_library_file(
                db_session,
                issue=issue,
                root=root,
                path=canonical_root_path / f"{prefix} {ordinal}.cbz",
            )
            issue_file_ids.append(int(library_file.id))
        canonical_ids.add(min(issue_file_ids))

    job = await _add_job(db_session)
    staged = await _add_staged_arc(
        db_session,
        job=job,
        name="Bounded Lookup",
        source_key="mylar3:bounded-library-file-lookup",
        source_arc_id="bounded-library-file-lookup",
        policy=_confirmed_policy(
            source="mylar3",
            mode="copy",
            target_library_root_id=root.id,
            destination_root=str(destination),
        ),
    )
    for ordinal, issue in enumerate((first_issue, second_issue), start=1):
        await _add_staged_entry(
            db_session,
            staged_arc=staged,
            source_ordinal=ordinal,
            reading_order=ordinal,
            issue_number_text=str(ordinal),
            matched_issue_id=issue.id,
        )
    job_id = int(job.id)
    db_session.expunge_all()

    loaded_library_file_ids: list[int] = []

    def _capture_loaded_library_files(_session: object, instance: object) -> None:
        if isinstance(instance, LibraryFile):
            loaded_library_file_ids.append(int(instance.id))

    event.listen(
        db_session.sync_session,
        "loaded_as_persistent",
        _capture_loaded_library_files,
    )
    try:
        await materialize_confirmed_story_arcs(
            db_session,
            import_job_id=job_id,
            record_action=record_action,
        )
    finally:
        event.remove(
            db_session.sync_session,
            "loaded_as_persistent",
            _capture_loaded_library_files,
        )

    assert set(loaded_library_file_ids) == canonical_ids
    assert len(loaded_library_file_ids) == len(canonical_ids)


@pytest.mark.asyncio
async def test_imported_reference_satisfies_initial_managed_policy_without_ownership_theft(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    canonical_root_path = tmp_path / "library"
    canonical_root_path.mkdir()
    destination = canonical_root_path / "Story Arcs"
    destination.mkdir()
    source_root = tmp_path / "mylar-export"
    source_root.mkdir()
    existing_arc_artifact = source_root / "Existing Arc" / "001 - Existing.cbz"
    existing_arc_artifact.parent.mkdir()
    existing_arc_artifact.write_bytes(b"pre-existing-user-artifact")
    root = LibraryRoot(name="Comics", path=str(canonical_root_path), enabled=True)
    db_session.add(root)
    await db_session.flush()
    issue = await _add_issue(
        db_session,
        series_name="Existing Series",
        issue_number=1.0,
        issue_number_text="1",
    )
    await _add_library_file(
        db_session,
        issue=issue,
        root=root,
        path=canonical_root_path / "Existing Series 001.cbz",
    )
    job = await _add_job(
        db_session,
        source_type=ImportSourceType.FILESYSTEM,
        source_path=str(source_root),
    )
    staged = await _add_staged_arc(
        db_session,
        job=job,
        name="Existing Arc",
        source_key="folder:existing-managed-policy",
        source_arc_id=None,
        source_kind=StoryArcSourceKind.FOLDER,
        policy=_confirmed_policy(
            source="folder",
            mode="copy",
            target_library_root_id=root.id,
            destination_root=str(destination),
            synchronize=True,
        ),
    )
    await _add_staged_entry(
        db_session,
        staged_arc=staged,
        source_ordinal=1,
        reading_order=1,
        issue_number_text="1",
        matched_issue_id=issue.id,
        cv_arc_id=None,
        source_location=str(existing_arc_artifact),
    )

    result = await materialize_confirmed_story_arcs(
        db_session,
        import_job_id=job.id,
        record_action=record_action,
    )

    placement = (await db_session.scalars(select(StoryArcPlacement))).one()
    actions = list(
        (await db_session.scalars(select(ImportJobAction).order_by(ImportJobAction.id))).all()
    )
    assert result.managed_placements_queued == 0
    assert result.managed_placements_reused == 1
    assert placement.ownership is StoryArcPlacementOwnership.REFERENCED
    assert placement.placement_path == str(existing_arc_artifact)
    assert await db_session.scalar(select(func.count()).select_from(StoryArcSyncWork)) == 0
    assert "story_arc_referenced_placement_attached" in {action.action_type for action in actions}
    assert "story_arc_managed_placement_requested" not in {action.action_type for action in actions}
    assert existing_arc_artifact.read_bytes() == b"pre-existing-user-artifact"


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
async def test_cancellation_checks_batches_without_committing_when_checkpoint_is_omitted(
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
