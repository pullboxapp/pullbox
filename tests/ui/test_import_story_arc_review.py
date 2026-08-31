"""Accessible Step 3 story-arc review UI contracts."""

from __future__ import annotations

import os
import sys

import pytest
from sqlalchemy import event

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytest_plugins = ["conftest_security"]


@pytest.mark.asyncio
async def test_story_arc_review_partial_is_paginated_and_keeps_arc_counts_separate(
    authenticated_client,
    sec_db,
) -> None:  # type: ignore[no-untyped-def]
    from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType
    from pullbox.models.story_arc import (
        ImportedStoryArcStatus,
        StoryArcResolutionState,
        StoryArcSourceKind,
    )
    from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry

    async with sec_db() as session:
        job = ImportJob(
            source_path="/tmp/mylar.db",
            source_type=ImportSourceType.MYLAR3,
            status=ImportJobStatus.REVIEW,
        )
        session.add(job)
        await session.flush()
        arc = ImportedStoryArc(
            import_job_id=job.id,
            source_kind=StoryArcSourceKind.MYLAR3,
            source_key="mylar3:knightfall",
            source_ordinal=7,
            name="Knightfall",
            status=ImportedStoryArcStatus.NEEDS_REVIEW,
        )
        session.add(arc)
        await session.flush()
        session.add_all(
            [
                ImportedStoryArcEntry(
                    imported_story_arc_id=arc.id,
                    source_ordinal=1,
                    reading_order=10,
                    reading_order_raw="010",
                    resolution_state=StoryArcResolutionState.RESOLVED,
                    source_kind=StoryArcSourceKind.MYLAR3,
                    source_series_name="Batman",
                    source_issue_number_text="497",
                ),
                ImportedStoryArcEntry(
                    imported_story_arc_id=arc.id,
                    source_ordinal=2,
                    reading_order=20,
                    reading_order_raw="020",
                    resolution_state=StoryArcResolutionState.MISSING,
                    source_kind=StoryArcSourceKind.MYLAR3,
                    source_series_name="Detective Comics",
                    source_issue_number_text="659",
                ),
            ]
        )
        await session.commit()
        job_id = job.id

    response = await authenticated_client.get(
        f"/import/{job_id}/review-partial?status=story_arcs&page=1"
    )

    assert response.status_code == 200
    assert 'data-testid="import-review-tab-story-arcs"' in response.text
    assert 'data-testid="import-review-story-arcs"' in response.text
    assert "Story arcs detected for this import" in response.text
    assert "Knightfall" in response.text
    assert "Mylar3" in response.text
    assert "Source order 7" in response.text
    assert "1 resolved" in response.text
    assert "1 missing" in response.text
    assert "0 conflicts" in response.text
    assert 'aria-label="Story arc decision for Knightfall"' in response.text
    assert "Create new story arc" in response.text
    assert 'data-testid="import-story-arc-select-' in response.text
    assert 'data-testid="import-story-arc-skip-' in response.text
    assert 'data-testid="import-story-arc-pagination"' in response.text
    assert 'data-testid="import-review-pagination"' not in response.text


@pytest.mark.asyncio
async def test_story_arc_review_context_does_not_place_arc_ids_in_series_selection(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType
    from pullbox.models.story_arc import ImportedStoryArcStatus, StoryArcSourceKind
    from pullbox.models.story_arc_import import ImportedStoryArc
    from pullbox.ui.import_review_context import load_import_review_context

    job = ImportJob(
        source_path="/tmp/import",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
    )
    db_session.add(job)
    await db_session.flush()
    arc = ImportedStoryArc(
        import_job_id=job.id,
        source_kind=StoryArcSourceKind.FOLDER,
        source_key="folder:test",
        source_ordinal=1,
        name="Crisis",
        status=ImportedStoryArcStatus.DETECTED,
    )
    db_session.add(arc)
    await db_session.flush()

    context = await load_import_review_context(
        db_session,
        job,
        status="story_arcs",
        page=1,
        sort=None,
    )

    assert context["current_view"] == "story_arcs"
    assert context["series_items"] == []
    assert context["selected_series_ids"] == []
    assert [item.id for item in context["story_arc_items"]] == [arc.id]
    assert context["story_arc_total"] == 1


@pytest.mark.asyncio
async def test_story_arc_step_three_shows_safe_draft_and_separate_policy_confirmation(
    authenticated_client,
    sec_db,
) -> None:  # type: ignore[no-untyped-def]
    from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType
    from pullbox.models.library import LibraryRoot
    from pullbox.models.story_arc import ImportedStoryArcStatus, StoryArcSourceKind
    from pullbox.models.story_arc_import import ImportedStoryArc

    async with sec_db() as session:
        job = ImportJob(
            source_path="/private/mylar.db",
            source_type=ImportSourceType.MYLAR3,
            status=ImportJobStatus.REVIEW,
            story_arc_import_requested=True,
            story_arc_materialization_requested=False,
        )
        root = LibraryRoot(name="Comics", path="/private/comics", enabled=True)
        session.add_all([job, root])
        await session.flush()
        arc = ImportedStoryArc(
            import_job_id=job.id,
            source_kind=StoryArcSourceKind.MYLAR3,
            source_key="mylar3:policy-ui",
            source_ordinal=1,
            name="Knightfall",
            status=ImportedStoryArcStatus.READY,
            selected_for_import=True,
            proposed_policy_snapshot={
                "schema_version": 1,
                "source": "mylar3",
                "activation": "requires_confirmation",
                "settings_present": True,
                "review_required": True,
                "review_warnings": ["legacy_move_mapped_to_copy"],
                "monitored": True,
                "search_missing": True,
                "include_upcoming": False,
                "sync_enabled": True,
                "placement_policy": {
                    "schema_version": 1,
                    "mode": "copy",
                    "target_library_root_id": None,
                    "destination_root": "/private/story-arcs",
                    "folder_template": "{Publisher} - {StoryArc}",
                    "file_template": "{ReadingOrder:03d} - {Series} {IssueNumber}",
                    "symlink_style": None,
                    "synchronize": True,
                },
            },
            source_settings_snapshot={
                "schema_version": 1,
                "present": True,
                "parse_warnings": [],
                "values": {
                    "STORYARCDIR": {"value": True},
                    "STORYARC_LOCATION": {"value": "/private/story-arcs"},
                    "COPY2ARCDIR": {"value": True},
                    "ARC_FILEOPS": {"value": "move"},
                },
            },
        )
        session.add(arc)
        await session.commit()
        job_id = int(job.id)
        arc_id = int(arc.id)

    response = await authenticated_client.get(
        f"/import/{job_id}/review-partial?status=story_arcs&story_arc_id={arc_id}"
    )

    assert response.status_code == 200
    assert 'data-testid="import-story-arc-policy-' in response.text
    assert "Import logical arc and memberships" in response.text
    assert "Create or reference a separate arc folder" in response.text
    assert 'data-testid="import-story-arc-step-one-intent"' in response.text
    assert "Logical arcs requested; separate arc files remain off" in response.text
    assert "Review each detected arc below" in response.text
    assert "Policy needs confirmation" in response.text
    assert "Legacy move mapped to copy" in response.text
    assert "Detected Mylar settings" in response.text
    assert "Destination configured" in response.text
    assert "{Publisher} - {StoryArc}" in response.text
    assert "Example Publisher - Example Arc" in response.text
    assert 'data-testid="import-story-arc-policy-confirm-' in response.text
    assert 'data-testid="import-story-arc-materialize-' in response.text
    assert 'data-testid="import-story-arc-policy-root-' in response.text
    assert "Comics" in response.text
    assert "/private/story-arcs" not in response.text
    assert "/private/comics" not in response.text


@pytest.mark.asyncio
async def test_story_arc_review_partial_shows_entry_evidence_without_private_paths(
    authenticated_client,
    sec_db,
) -> None:  # type: ignore[no-untyped-def]
    from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType
    from pullbox.models.issue import Issue
    from pullbox.models.series import Series
    from pullbox.models.story_arc import (
        ImportedStoryArcStatus,
        StoryArcResolutionState,
        StoryArcSourceKind,
    )
    from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry

    async with sec_db() as session:
        job = ImportJob(
            source_path="/tmp/mylar.db",
            source_type=ImportSourceType.MYLAR3,
            status=ImportJobStatus.REVIEW,
        )
        series = Series(title="Batman", sort_title="Batman", issue_count=1)
        session.add_all([job, series])
        await session.flush()
        issue = Issue(
            series_id=series.id,
            issue_number=1_000_000,
            issue_number_text="1000000",
            title="The Last Case",
        )
        arc = ImportedStoryArc(
            import_job_id=job.id,
            source_kind=StoryArcSourceKind.MYLAR3,
            source_key="mylar3:future-state",
            source_ordinal=9,
            name="Future State",
            status=ImportedStoryArcStatus.NEEDS_REVIEW,
        )
        session.add_all([issue, arc])
        await session.flush()
        session.add_all(
            [
                ImportedStoryArcEntry(
                    imported_story_arc_id=arc.id,
                    matched_issue_id=issue.id,
                    source_ordinal=1,
                    reading_order=10,
                    reading_order_raw="010",
                    resolution_state=StoryArcResolutionState.RESOLVED,
                    source_kind=StoryArcSourceKind.MYLAR3,
                    source_series_name="Batman",
                    source_issue_number_text="1000000",
                    source_issue_title="The Last Case",
                    source_location="/mnt/user/private/Batman 1000000.cbz",
                    selected_for_import=True,
                ),
                ImportedStoryArcEntry(
                    imported_story_arc_id=arc.id,
                    source_ordinal=2,
                    reading_order=20,
                    reading_order_raw="020",
                    resolution_state=StoryArcResolutionState.CONFLICT,
                    source_kind=StoryArcSourceKind.MYLAR3,
                    source_series_name="Batman Annual",
                    source_issue_number_text="1AU",
                    source_issue_title="Aftermath",
                    source_location="/mnt/user/private/Batman Annual 1AU.cbz",
                    selected_for_import=False,
                ),
                ImportedStoryArcEntry(
                    imported_story_arc_id=arc.id,
                    source_ordinal=3,
                    reading_order=None,
                    reading_order_raw=None,
                    resolution_state=StoryArcResolutionState.MISSING,
                    source_kind=StoryArcSourceKind.MYLAR3,
                    source_series_name="Batman",
                    source_issue_number_text="0.5",
                    source_issue_title="Prelude",
                    source_location=None,
                    selected_for_import=False,
                ),
            ]
        )
        await session.commit()
        job_id = job.id
        arc_id = arc.id

    response = await authenticated_client.get(
        f"/import/{job_id}/review-partial",
        params={
            "status": "story_arcs",
            "page": 1,
            "story_arc_id": arc_id,
            "arc_entry_state": "all",
            "arc_entry_page": 1,
        },
    )

    assert response.status_code == 200
    assert 'data-testid="import-story-arc-entry-review"' in response.text
    assert 'data-testid="import-story-arc-entry-filter"' in response.text
    assert "Source order 1" in response.text
    assert "Reading order 010" in response.text
    assert "1000000" in response.text
    assert "1AU" in response.text
    assert "0.5" in response.text
    assert "Batman" in response.text
    assert "The Last Case" in response.text
    assert "Matched to Batman #1000000" in response.text
    assert "Conflict" in response.text
    assert "Missing" in response.text
    assert "Source location recorded" in response.text
    assert "No source location recorded" in response.text
    assert "Selected" in response.text
    assert "Skipped" in response.text
    assert "Needs attention: 1 missing, 1 conflict." in response.text
    assert "/mnt/user/private" not in response.text


@pytest.mark.asyncio
async def test_story_arc_entry_filter_and_pagination_preserve_arc_page(
    authenticated_client,
    sec_db,
) -> None:  # type: ignore[no-untyped-def]
    from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType
    from pullbox.models.story_arc import (
        ImportedStoryArcStatus,
        StoryArcResolutionState,
        StoryArcSourceKind,
    )
    from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry

    async with sec_db() as session:
        job = ImportJob(
            source_path="/tmp/mylar.db",
            source_type=ImportSourceType.MYLAR3,
            status=ImportJobStatus.REVIEW,
        )
        session.add(job)
        await session.flush()
        arc = ImportedStoryArc(
            import_job_id=job.id,
            source_kind=StoryArcSourceKind.MYLAR3,
            source_key="mylar3:long-arc",
            source_ordinal=1,
            name="Long Arc",
            status=ImportedStoryArcStatus.NEEDS_REVIEW,
        )
        session.add(arc)
        await session.flush()
        session.add(
            ImportedStoryArcEntry(
                imported_story_arc_id=arc.id,
                source_ordinal=1,
                reading_order=1,
                reading_order_raw="001",
                resolution_state=StoryArcResolutionState.RESOLVED,
                source_kind=StoryArcSourceKind.MYLAR3,
                source_series_name="Resolved Series",
                source_issue_number_text="1000000",
            )
        )
        session.add_all(
            [
                ImportedStoryArcEntry(
                    imported_story_arc_id=arc.id,
                    source_ordinal=index + 1,
                    reading_order=index,
                    reading_order_raw=f"{index:03d}",
                    resolution_state=StoryArcResolutionState.MISSING,
                    source_kind=StoryArcSourceKind.MYLAR3,
                    source_series_name="Missing Series",
                    source_issue_number_text=f"M{index}",
                )
                for index in range(1, 27)
            ]
        )
        await session.commit()
        job_id = job.id
        arc_id = arc.id

    response = await authenticated_client.get(
        f"/import/{job_id}/review-partial",
        params={
            "status": "story_arcs",
            "page": 1,
            "story_arc_id": arc_id,
            "arc_entry_state": "missing",
            "arc_entry_page": 2,
        },
    )

    assert response.status_code == 200
    assert "M26" in response.text
    assert "M25" not in response.text
    assert "1000000" not in response.text
    assert 'data-testid="import-story-arc-entry-pagination"' in response.text
    assert "status=story_arcs" in response.text
    assert "page=1" in response.text
    assert f"story_arc_id={arc_id}" in response.text
    assert "arc_entry_state=missing" in response.text
    assert "arc_entry_page=1" in response.text


@pytest.mark.asyncio
async def test_story_arc_entry_review_explains_empty_arc_and_empty_filter(
    authenticated_client,
    sec_db,
) -> None:  # type: ignore[no-untyped-def]
    from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType
    from pullbox.models.story_arc import (
        ImportedStoryArcStatus,
        StoryArcResolutionState,
        StoryArcSourceKind,
    )
    from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry

    async with sec_db() as session:
        job = ImportJob(
            source_path="/tmp/mylar.db",
            source_type=ImportSourceType.MYLAR3,
            status=ImportJobStatus.REVIEW,
        )
        session.add(job)
        await session.flush()
        empty_arc = ImportedStoryArc(
            import_job_id=job.id,
            source_kind=StoryArcSourceKind.MYLAR3,
            source_key="mylar3:empty",
            source_ordinal=1,
            name="Empty Arc",
            status=ImportedStoryArcStatus.NEEDS_REVIEW,
        )
        resolved_arc = ImportedStoryArc(
            import_job_id=job.id,
            source_kind=StoryArcSourceKind.MYLAR3,
            source_key="mylar3:resolved",
            source_ordinal=2,
            name="Resolved Arc",
            status=ImportedStoryArcStatus.NEEDS_REVIEW,
        )
        session.add_all([empty_arc, resolved_arc])
        await session.flush()
        session.add(
            ImportedStoryArcEntry(
                imported_story_arc_id=resolved_arc.id,
                source_ordinal=1,
                reading_order=1,
                reading_order_raw="001",
                resolution_state=StoryArcResolutionState.RESOLVED,
                source_kind=StoryArcSourceKind.MYLAR3,
                source_series_name="Resolved Series",
                source_issue_number_text="1",
            )
        )
        await session.commit()
        job_id = job.id
        empty_arc_id = empty_arc.id
        resolved_arc_id = resolved_arc.id

    empty_response = await authenticated_client.get(
        f"/import/{job_id}/review-partial",
        params={
            "status": "story_arcs",
            "story_arc_id": empty_arc_id,
            "arc_entry_state": "all",
        },
    )
    filtered_response = await authenticated_client.get(
        f"/import/{job_id}/review-partial",
        params={
            "status": "story_arcs",
            "story_arc_id": resolved_arc_id,
            "arc_entry_state": "missing",
        },
    )

    assert empty_response.status_code == 200
    assert "This story arc has no entries to review." in empty_response.text
    assert "You can skip the arc or return after its source data is fixed." in empty_response.text
    assert filtered_response.status_code == 200
    assert "No missing entries in this story arc." in filtered_response.text
    assert "Choose All states to review every entry." in filtered_response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"story_arc_id": 0}, "story_arc_id"),
        ({"arc_entry_state": "not-a-state"}, "arc_entry_state"),
        ({"arc_entry_page": 0}, "arc_entry_page"),
    ],
)
async def test_story_arc_review_entry_query_parameters_are_validated(
    authenticated_client,
    params: dict[str, object],
    field: str,
) -> None:  # type: ignore[no-untyped-def]
    response = await authenticated_client.get(
        "/import/1/review-partial",
        params={"status": "story_arcs", **params},
    )

    assert response.status_code == 422
    assert field in response.text


@pytest.mark.asyncio
async def test_story_arc_entry_review_query_count_does_not_grow_with_entries(
    db_session,
    async_engine,
) -> None:  # type: ignore[no-untyped-def]
    from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType
    from pullbox.models.story_arc import (
        ImportedStoryArcStatus,
        StoryArcResolutionState,
        StoryArcSourceKind,
    )
    from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
    from pullbox.ui.import_story_arc_entry_review import load_import_story_arc_entry_review_page

    job = ImportJob(
        source_path="/tmp/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.REVIEW,
    )
    db_session.add(job)
    await db_session.flush()
    arc = ImportedStoryArc(
        import_job_id=job.id,
        source_kind=StoryArcSourceKind.MYLAR3,
        source_key="mylar3:scale",
        source_ordinal=1,
        name="Scale Arc",
        status=ImportedStoryArcStatus.NEEDS_REVIEW,
    )
    db_session.add(arc)
    await db_session.flush()
    db_session.add_all(
        [
            ImportedStoryArcEntry(
                imported_story_arc_id=arc.id,
                source_ordinal=index,
                reading_order=index,
                reading_order_raw=str(index),
                resolution_state=StoryArcResolutionState.MISSING,
                source_kind=StoryArcSourceKind.MYLAR3,
                source_series_name="Scale Series",
                source_issue_number_text=str(index),
            )
            for index in range(1, 206)
        ]
    )
    await db_session.flush()

    selects: list[str] = []

    def record_select(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(async_engine.sync_engine, "before_cursor_execute", record_select)
    try:
        page = await load_import_story_arc_entry_review_page(
            db_session,
            job_id=job.id,
            imported_story_arc_id=arc.id,
            resolution_state=StoryArcResolutionState.MISSING,
            page=5,
            page_size=25,
        )
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", record_select)

    assert page.total == 205
    assert len(page.items) == 25
    assert len(selects) <= 2, f"entry review issued {len(selects)} SELECT statements"
