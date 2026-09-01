"""Step 3 story-arc review and confirmation contracts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import func, select

from pullbox.core.exceptions import ValidationError
from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType
from pullbox.models.library import LibraryRoot
from pullbox.models.story_arc import (
    ImportedStoryArcStatus,
    IssueStoryArc,
    StoryArc,
    StoryArcLifecycle,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.schemas.import_job import ConfirmImportRequest, StoryArcReviewDecision
from pullbox.services import library_root_management


@pytest.fixture
async def managed_import_root(
    db_session: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> LibraryRoot:
    """Provide the managed destination required by direct job fixtures."""
    root_path = tmp_path / "story-arc-review-library"
    root_path.mkdir()
    root = LibraryRoot(
        name="Story arc review library",
        path=str(root_path),
        enabled=True,
        allow_referenced_registrations=True,
        allow_managed_writes=True,
        is_default_managed_destination=True,
    )
    db_session.add(root)
    await db_session.flush()
    monkeypatch.setattr(
        library_root_management.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=100 * 1024**3),
    )
    return root


async def _stage_arc(
    db_session: Any,
    job: ImportJob,
    *,
    name: str = "Knightfall",
    source_ordinal: int = 1,
    status: ImportedStoryArcStatus = ImportedStoryArcStatus.DETECTED,
    states: tuple[StoryArcResolutionState, ...] = (StoryArcResolutionState.RESOLVED,),
    safety_incomplete: bool = False,
) -> ImportedStoryArc:
    staged = ImportedStoryArc(
        import_job_id=job.id,
        source_kind=StoryArcSourceKind.MYLAR3,
        source_key=f"mylar3:{name}:{source_ordinal}",
        source_ordinal=source_ordinal,
        name=name,
        status=status,
        diagnostics={"safety_incomplete": safety_incomplete},
    )
    db_session.add(staged)
    await db_session.flush()
    for entry_ordinal, state in enumerate(states, start=1):
        db_session.add(
            ImportedStoryArcEntry(
                imported_story_arc_id=staged.id,
                source_ordinal=entry_ordinal,
                reading_order=entry_ordinal * 10,
                reading_order_raw=str(entry_ordinal * 10),
                resolution_state=state,
                source_kind=StoryArcSourceKind.MYLAR3,
                source_issue_number_text=str(entry_ordinal),
                source_series_name=f"Series {entry_ordinal}",
                selected_for_import=False,
                diagnostics={},
            )
        )
    await db_session.flush()
    return staged


def test_confirm_schema_adds_deduplicated_story_arc_decisions_without_reusing_series_ids() -> None:
    request = ConfirmImportRequest(
        series_ids=[],
        story_arc_ids=[9, 9, 12],
        story_arc_decisions=[
            StoryArcReviewDecision(
                imported_story_arc_id=9,
                action="select",
                proposed_story_arc_id=4,
            ),
            StoryArcReviewDecision(imported_story_arc_id=12, action="skip"),
        ],
    )

    assert request.series_ids == []
    assert request.story_arc_ids == [9, 12]
    assert [decision.imported_story_arc_id for decision in request.story_arc_decisions] == [9, 12]

    with pytest.raises(PydanticValidationError, match="proposed_story_arc_id"):
        StoryArcReviewDecision(
            imported_story_arc_id=12,
            action="skip",
            proposed_story_arc_id=4,
        )


@pytest.mark.asyncio
async def test_story_arc_select_and_skip_persist_only_staging_decisions(db_session: Any) -> None:
    from pullbox.services.import_story_arc_review import update_import_story_arc_decision

    job = ImportJob(
        source_path="/tmp/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.REVIEW,
    )
    target = StoryArc(name="Knightfall")
    db_session.add_all([job, target])
    await db_session.flush()
    staged = await _stage_arc(
        db_session,
        job,
        states=(StoryArcResolutionState.RESOLVED, StoryArcResolutionState.MISSING),
    )

    selected = await update_import_story_arc_decision(
        db_session,
        job.id,
        staged.id,
        action="select",
        proposed_story_arc_id=target.id,
    )

    assert selected.status == ImportedStoryArcStatus.READY
    assert selected.selected_for_import is True
    assert selected.proposed_story_arc_id == target.id
    entries = list(
        (
            await db_session.execute(
                select(ImportedStoryArcEntry).where(
                    ImportedStoryArcEntry.imported_story_arc_id == staged.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert [entry.selected_for_import for entry in entries] == [True, True]
    assert await db_session.scalar(select(func.count(StoryArc.id))) == 1
    assert await db_session.scalar(select(func.count(IssueStoryArc.id))) == 0

    skipped = await update_import_story_arc_decision(
        db_session,
        job.id,
        staged.id,
        action="skip",
        proposed_story_arc_id=None,
    )
    assert skipped.status == ImportedStoryArcStatus.SKIPPED
    assert skipped.selected_for_import is False
    assert skipped.proposed_story_arc_id is None
    assert all(entry.selected_for_import is False for entry in entries)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("states", "safety_incomplete", "message"),
    [
        ((StoryArcResolutionState.CONFLICT,), False, "conflict"),
        ((StoryArcResolutionState.RESOLVED,), True, "safety"),
    ],
)
async def test_story_arc_selection_fails_closed_for_conflict_and_safety_rows(
    db_session: Any,
    states: tuple[StoryArcResolutionState, ...],
    safety_incomplete: bool,
    message: str,
) -> None:
    from pullbox.services.import_story_arc_review import update_import_story_arc_decision

    job = ImportJob(
        source_path="/tmp/import",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
    )
    db_session.add(job)
    await db_session.flush()
    staged = await _stage_arc(
        db_session,
        job,
        states=states,
        safety_incomplete=safety_incomplete,
    )

    with pytest.raises(ValidationError, match=message):
        await update_import_story_arc_decision(
            db_session,
            job.id,
            staged.id,
            action="select",
            proposed_story_arc_id=None,
        )

    assert staged.status in {
        ImportedStoryArcStatus.DETECTED,
        ImportedStoryArcStatus.NEEDS_REVIEW,
    }
    assert staged.selected_for_import is False


@pytest.mark.asyncio
async def test_story_arc_review_page_is_paginated_and_preserves_order_and_counts(
    db_session: Any,
) -> None:
    from pullbox.services.import_story_arc_review import load_import_story_arc_review_page

    job = ImportJob(
        source_path="/tmp/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.REVIEW,
    )
    db_session.add(job)
    await db_session.flush()
    for ordinal in range(1, 27):
        states = (
            StoryArcResolutionState.RESOLVED,
            StoryArcResolutionState.MISSING,
            StoryArcResolutionState.CONFLICT,
        )
        await _stage_arc(
            db_session,
            job,
            name=f"Arc {ordinal:02d}",
            source_ordinal=ordinal,
            status=ImportedStoryArcStatus.NEEDS_REVIEW,
            states=states,
        )

    page = await load_import_story_arc_review_page(
        db_session,
        job.id,
        page=2,
        page_size=25,
    )

    assert page.total == 26
    assert page.page == 2
    assert page.page_size == 25
    assert [item.name for item in page.items] == ["Arc 26"]
    row = page.items[0]
    assert row.source_kind == StoryArcSourceKind.MYLAR3
    assert row.source_ordinal == 26
    assert row.entries_total == 3
    assert row.entries_resolved == 1
    assert row.entries_missing == 1
    assert row.entries_conflict == 1
    assert row.selection_blocked is True


@pytest.mark.asyncio
async def test_review_summary_keeps_story_arc_counts_out_of_series_totals(db_session: Any) -> None:
    from pullbox.ui.import_review_summary import load_import_review_summary

    job = ImportJob(
        source_path="/tmp/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.REVIEW,
    )
    db_session.add(job)
    await db_session.flush()
    staged = await _stage_arc(
        db_session,
        job,
        states=(StoryArcResolutionState.RESOLVED, StoryArcResolutionState.MISSING),
    )
    staged.status = ImportedStoryArcStatus.READY
    staged.selected_for_import = True
    await db_session.flush()

    summary = await load_import_review_summary(db_session, job)

    assert summary["series_total"] == 0
    assert summary["story_arcs_total"] == 1
    assert summary["story_arcs_selected"] == 1
    assert summary["story_arc_entries_total"] == 2
    assert summary["story_arc_entries_resolved"] == 1
    assert summary["story_arc_entries_missing"] == 1
    assert summary["selected_series_total"] == 0
    assert summary["selected_items_total"] == 1


@pytest.mark.asyncio
async def test_arc_only_confirmation_allows_canonical_issues_without_series_or_file_selection(
    db_session: Any,
    managed_import_root: LibraryRoot,
) -> None:
    from pullbox.services.import_confirmation import confirm_import_job

    job = ImportJob(
        source_path="/tmp/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.REVIEW,
        target_library_root_id=managed_import_root.id,
    )
    db_session.add(job)
    await db_session.flush()
    staged = await _stage_arc(
        db_session,
        job,
        states=(StoryArcResolutionState.RESOLVED, StoryArcResolutionState.MISSING),
    )

    async def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _manual_match(*_args: Any, **_kwargs: Any) -> None:
        return None

    result = await confirm_import_job(
        db_session,
        job.id,
        ConfirmImportRequest(series_ids=[], story_arc_ids=[staged.id]),
        apply_manual_file_match=_manual_match,
        recompute_file_counters=_noop,
        apply_confirm_policy=_noop,
        log_event=_noop,
    )

    assert result.status == ImportJobStatus.IMPORTING
    assert staged.status == ImportedStoryArcStatus.CONFIRMED
    assert staged.selected_for_import is True
    assert await db_session.scalar(select(func.count(StoryArc.id))) == 0
    assert await db_session.scalar(select(func.count(IssueStoryArc.id))) == 0


@pytest.mark.asyncio
async def test_compatibility_story_arc_ids_add_to_existing_durable_selection(
    db_session: Any,
) -> None:
    from pullbox.services.import_story_arc_review import (
        confirm_import_story_arcs,
        update_import_story_arc_decision,
    )

    job = ImportJob(
        source_path="/tmp/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.REVIEW,
    )
    db_session.add(job)
    await db_session.flush()
    first = await _stage_arc(db_session, job, name="Knightfall", source_ordinal=1)
    second = await _stage_arc(db_session, job, name="No Man's Land", source_ordinal=2)
    await update_import_story_arc_decision(
        db_session,
        job.id,
        first.id,
        action="select",
        proposed_story_arc_id=None,
    )

    confirmed_count = await confirm_import_story_arcs(
        db_session,
        job.id,
        story_arc_ids=[second.id],
        decisions=[],
    )

    assert confirmed_count == 2
    assert first.status == ImportedStoryArcStatus.CONFIRMED
    assert second.status == ImportedStoryArcStatus.CONFIRMED


@pytest.mark.asyncio
async def test_archived_story_arc_is_not_offered_or_accepted_as_merge_target(
    db_session: Any,
) -> None:
    from pullbox.services.import_story_arc_review import (
        load_import_story_arc_review_page,
        update_import_story_arc_decision,
    )

    job = ImportJob(
        source_path="/tmp/import",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
    )
    archived = StoryArc(name="Knightfall", lifecycle=StoryArcLifecycle.ARCHIVED)
    db_session.add_all([job, archived])
    await db_session.flush()
    staged = await _stage_arc(db_session, job, name="Knightfall")

    page = await load_import_story_arc_review_page(db_session, job.id)

    assert page.items[0].merge_candidates == ()
    with pytest.raises(ValidationError, match="archived"):
        await update_import_story_arc_decision(
            db_session,
            job.id,
            staged.id,
            action="select",
            proposed_story_arc_id=archived.id,
        )
    assert staged.status == ImportedStoryArcStatus.DETECTED
    assert staged.selected_for_import is False


@pytest.mark.asyncio
async def test_confirmation_revalidates_a_persisted_merge_target_lifecycle(
    db_session: Any,
) -> None:
    from pullbox.services.import_story_arc_review import (
        confirm_import_story_arcs,
        update_import_story_arc_decision,
    )

    job = ImportJob(
        source_path="/tmp/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.REVIEW,
    )
    target = StoryArc(name="Knightfall")
    db_session.add_all([job, target])
    await db_session.flush()
    staged = await _stage_arc(db_session, job, name="Knightfall")
    await update_import_story_arc_decision(
        db_session,
        job.id,
        staged.id,
        action="select",
        proposed_story_arc_id=target.id,
    )
    target.lifecycle = StoryArcLifecycle.ARCHIVED
    await db_session.flush()

    with pytest.raises(ValidationError, match="archived"):
        await confirm_import_story_arcs(
            db_session,
            job.id,
            story_arc_ids=[],
            decisions=[],
        )

    assert staged.status == ImportedStoryArcStatus.READY
