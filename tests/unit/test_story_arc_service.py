"""Logical story-arc management stays provider-free and preserves canonical issues."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import func, select

from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.publisher import Publisher
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArcPlacement,
    StoryArcPlacementMode,
    StoryArcPlacementOwnership,
)
from pullbox.services.story_arc_service import (
    DuplicateStoryArcMembershipError,
    StoryArcConflictError,
    StoryArcService,
    StoryArcValidationError,
)


async def _issue(
    db_session,
    *,
    series_name: str,
    number: float,
    exact_number: str,
) -> Issue:
    publisher = Publisher(name=f"{series_name} Publisher")
    series = Series(title=series_name, sort_title=series_name)
    series.publisher = publisher
    issue = Issue(
        series=series,
        issue_number=number,
        issue_number_text=exact_number,
    )
    db_session.add(issue)
    await db_session.flush()
    return issue


async def test_create_update_archive_manages_monitoring_and_revision(db_session) -> None:
    service = StoryArcService()
    arc = await service.create(
        db_session,
        name="  Absolute\tPower ",
        description="Initial description",
        monitored=True,
        search_missing=True,
        include_upcoming=True,
        sync_enabled=True,
    )

    assert arc.name == "Absolute Power"
    assert arc.normalized_name == "absolute power"
    assert arc.lifecycle.value == "active"
    assert arc.monitored is True
    assert arc.search_missing is True
    assert arc.include_upcoming is True
    assert arc.sync_enabled is True
    assert arc.revision == 1

    updated = await service.update(
        db_session,
        arc.id,
        expected_revision=1,
        name="Absolute Power",
        description=None,
        monitored=False,
        search_missing=False,
        include_upcoming=False,
        sync_enabled=False,
    )
    assert updated.description is None
    assert updated.monitored is False
    assert updated.search_missing is False
    assert updated.include_upcoming is False
    assert updated.sync_enabled is False
    assert updated.revision == 2

    archived = await service.archive(db_session, arc.id, expected_revision=2)
    assert archived.lifecycle.value == "archived"
    assert archived.monitored is False
    assert archived.search_missing is False
    assert archived.include_upcoming is False
    assert archived.sync_enabled is False
    assert archived.revision == 3

    with pytest.raises(StoryArcConflictError, match="revision"):
        await service.update(
            db_session,
            arc.id,
            expected_revision=2,
            monitored=True,
        )


async def test_create_keeps_same_named_arcs_distinct_without_auto_merging(db_session) -> None:
    service = StoryArcService()
    first = await service.create(db_session, name="Crisis on Infinite Earths")
    second = await service.create(db_session, name="  CRISIS  ON INFINITE EARTHS ")

    assert first.id != second.id
    assert first.normalized_name == second.normalized_name


async def test_one_canonical_issue_can_belong_to_multiple_arcs(db_session) -> None:
    service = StoryArcService()
    issue = await _issue(
        db_session,
        series_name="Batman",
        number=1,
        exact_number="1",
    )
    first_arc = await service.create(db_session, name="Court of Owls")
    second_arc = await service.create(db_session, name="Night of the Owls")

    first = await service.add_membership(
        db_session,
        first_arc.id,
        issue_id=issue.id,
        sequence_number=1,
        source_issue_number_text="1",
    )
    second = await service.add_membership(
        db_session,
        second_arc.id,
        issue_id=issue.id,
        sequence_number=7,
        source_issue_number_text="1",
    )

    assert first.issue_id == second.issue_id == issue.id
    assert first.story_arc_id != second.story_arc_id
    assert await db_session.scalar(select(func.count(Issue.id))) == 1
    assert await db_session.scalar(select(func.count(IssueStoryArc.id))) == 2


async def test_arc_monitoring_keeps_canonical_issue_state_independent_from_series_monitoring(
    db_session,
) -> None:
    service = StoryArcService()
    issue = await _issue(
        db_session,
        series_name="Unmonitored Series",
        number=1,
        exact_number="1",
    )
    issue.series.monitored = False
    issue.status = IssueStatus.SKIPPED
    arc = await service.create(
        db_session,
        name="Monitored Arc",
        monitored=True,
        search_missing=True,
    )

    membership = await service.add_membership(
        db_session,
        arc.id,
        issue_id=issue.id,
        sequence_number=1,
    )

    assert membership.sync_eligible is False
    assert issue.status == IssueStatus.SKIPPED


async def test_enabling_arc_search_preserves_existing_canonical_skip_state(
    db_session,
) -> None:
    service = StoryArcService()
    searchable = await _issue(
        db_session,
        series_name="Searchable Series",
        number=1,
        exact_number="1",
    )
    manually_skipped = await _issue(
        db_session,
        series_name="Manual Skip Series",
        number=2,
        exact_number="2",
    )
    searchable.status = IssueStatus.SKIPPED
    manually_skipped.status = IssueStatus.SKIPPED
    manually_skipped.manual_skip = True
    arc = await service.create(db_session, name="Deferred Monitoring")
    await service.add_membership(
        db_session,
        arc.id,
        issue_id=searchable.id,
        sequence_number=1,
    )
    await service.add_membership(
        db_session,
        arc.id,
        issue_id=manually_skipped.id,
        sequence_number=2,
    )

    await service.update(
        db_session,
        arc.id,
        expected_revision=3,
        monitored=True,
        search_missing=True,
    )

    assert searchable.status == IssueStatus.SKIPPED
    assert manually_skipped.status == IssueStatus.SKIPPED


async def test_arc_sync_toggle_updates_resolved_membership_eligibility(db_session) -> None:
    service = StoryArcService()
    issue = await _issue(
        db_session,
        series_name="Synchronization Series",
        number=1,
        exact_number="1",
    )
    arc = await service.create(db_session, name="Synchronization Arc")
    membership = await service.add_membership(
        db_session,
        arc.id,
        issue_id=issue.id,
        sequence_number=1,
    )
    assert membership.sync_eligible is False

    await service.update(
        db_session,
        arc.id,
        expected_revision=2,
        sync_enabled=True,
    )
    await db_session.refresh(membership)
    assert membership.sync_eligible is True

    await service.update(
        db_session,
        arc.id,
        expected_revision=3,
        sync_enabled=False,
    )
    await db_session.refresh(membership)
    assert membership.sync_eligible is False


async def test_add_membership_is_idempotent_but_rejects_conflicting_duplicate(db_session) -> None:
    service = StoryArcService()
    issue = await _issue(
        db_session,
        series_name="Superman",
        number=1000000,
        exact_number="1000000",
    )
    arc = await service.create(db_session, name="DC One Million")

    first = await service.add_membership(
        db_session,
        arc.id,
        issue_id=issue.id,
        sequence_number=4,
        source_ordinal=9,
        source_issue_number_text="1000000",
    )
    retry = await service.add_membership(
        db_session,
        arc.id,
        issue_id=issue.id,
        sequence_number=4,
        source_ordinal=9,
        source_issue_number_text="1000000",
    )

    assert retry.id == first.id
    assert await db_session.scalar(select(func.count(IssueStoryArc.id))) == 1

    with pytest.raises(DuplicateStoryArcMembershipError, match="already belongs"):
        await service.add_membership(
            db_session,
            arc.id,
            issue_id=issue.id,
            sequence_number=5,
            source_ordinal=9,
            source_issue_number_text="1000000",
        )


async def test_unresolved_entries_preserve_exact_numbers_and_deterministic_order(
    db_session,
) -> None:
    service = StoryArcService()
    arc = await service.create(db_session, name="Unresolved Arc")

    million = await service.add_membership(
        db_session,
        arc.id,
        issue_id=None,
        sequence_number=20,
        source_ordinal=2,
        source_issue_number_text="1000000",
    )
    annual = await service.add_membership(
        db_session,
        arc.id,
        issue_id=None,
        sequence_number=10,
        source_ordinal=2,
        source_issue_number_text="1AU",
    )
    fractional = await service.add_membership(
        db_session,
        arc.id,
        issue_id=None,
        sequence_number=10,
        source_ordinal=1,
        source_issue_number_text="0.5",
    )

    memberships = await service.list_memberships(db_session, arc.id)

    assert [entry.id for entry in memberships] == [fractional.id, annual.id, million.id]
    assert [entry.source_issue_number_text for entry in memberships] == [
        "0.5",
        "1AU",
        "1000000",
    ]
    assert all(entry.resolution_state.value == "missing" for entry in memberships)


async def test_resolve_and_reconcile_deleted_issue_retains_reviewable_entry(db_session) -> None:
    service = StoryArcService()
    issue = await _issue(
        db_session,
        series_name="Batman Annual",
        number=1,
        exact_number="1AU",
    )
    arc = await service.create(db_session, name="Annual Arc")
    membership = await service.add_membership(
        db_session,
        arc.id,
        issue_id=None,
        sequence_number=1,
        source_issue_number_text="1AU",
    )

    resolved = await service.resolve_membership(
        db_session,
        membership.id,
        issue_id=issue.id,
    )
    assert resolved.issue_id == issue.id
    assert resolved.resolution_state.value == "resolved"

    await service.detach_deleted_issue(db_session, issue.id)
    await db_session.delete(issue)
    await db_session.flush()
    await db_session.refresh(membership)

    assert membership.issue_id is None
    assert membership.resolution_state.value == "missing"
    assert membership.source_issue_number_text == "1AU"


async def test_reconcile_repairs_resolved_state_after_foreign_key_cleanup(db_session) -> None:
    service = StoryArcService()
    issue = await _issue(
        db_session,
        series_name="Recovered Series",
        number=1000000,
        exact_number="1000000",
    )
    arc = await service.create(db_session, name="Recovered Arc")
    membership = await service.add_membership(
        db_session,
        arc.id,
        issue_id=issue.id,
        sequence_number=1,
        source_issue_number_text="1000000",
    )
    membership.issue_id = None
    await db_session.flush()

    repaired = await service.reconcile_missing_issue_references(
        db_session,
        story_arc_id=arc.id,
    )

    assert repaired == 1
    assert membership.issue_id is None
    assert membership.resolution_state.value == "missing"
    assert membership.source_issue_number_text == "1000000"
    assert arc.revision == 3


async def test_resolving_to_an_existing_arc_issue_rejects_duplicate(db_session) -> None:
    service = StoryArcService()
    issue = await _issue(
        db_session,
        series_name="Detective Comics",
        number=0.5,
        exact_number="0.5",
    )
    arc = await service.create(db_session, name="Duplicate Resolution")
    await service.add_membership(
        db_session,
        arc.id,
        issue_id=issue.id,
        sequence_number=1,
        source_issue_number_text="0.5",
    )
    unresolved = await service.add_membership(
        db_session,
        arc.id,
        issue_id=None,
        sequence_number=2,
        source_ordinal=2,
        source_issue_number_text="0.5",
    )

    with pytest.raises(DuplicateStoryArcMembershipError, match="already belongs"):
        await service.resolve_membership(
            db_session,
            unresolved.id,
            issue_id=issue.id,
        )


async def test_update_remove_and_reorder_memberships_preserve_canonical_issue(
    db_session,
) -> None:
    service = StoryArcService()
    first_issue = await _issue(
        db_session,
        series_name="Series A",
        number=1,
        exact_number="1",
    )
    second_issue = await _issue(
        db_session,
        series_name="Series B",
        number=2,
        exact_number="2",
    )
    arc = await service.create(db_session, name="Mutable Arc")
    first = await service.add_membership(
        db_session,
        arc.id,
        issue_id=first_issue.id,
        sequence_number=10,
        source_issue_number_text="1",
    )
    second = await service.add_membership(
        db_session,
        arc.id,
        issue_id=second_issue.id,
        sequence_number=20,
        source_issue_number_text="2",
    )

    changed = await service.update_membership(
        db_session,
        first.id,
        sequence_number=30,
        intentionally_skipped=True,
    )
    assert changed.sequence_number == 30
    assert changed.resolution_state.value == "skipped"

    reordered = await service.reorder_memberships(
        db_session,
        arc.id,
        ordered_membership_ids=[first.id, second.id],
        expected_revision=4,
    )
    assert [entry.id for entry in reordered] == [first.id, second.id]
    assert [entry.sequence_number for entry in reordered] == [1, 2]

    with pytest.raises(StoryArcValidationError, match="exactly once"):
        await service.reorder_memberships(
            db_session,
            arc.id,
            ordered_membership_ids=[first.id],
            expected_revision=5,
        )

    await service.remove_membership(db_session, second.id)

    assert await db_session.get(Issue, second_issue.id) is second_issue
    assert await db_session.get(IssueStoryArc, second.id) is None
    assert await db_session.scalar(select(func.count(Issue.id))) == 2


async def test_managed_placement_blocks_mutations_without_a_filesystem_plan(
    db_session,
) -> None:
    service = StoryArcService()
    issue = await _issue(
        db_session,
        series_name="Batman",
        number=1,
        exact_number="1",
    )
    arc = await service.create(db_session, name="Court of Owls")
    membership = await service.add_membership(
        db_session,
        arc.id,
        issue_id=issue.id,
        sequence_number=1,
    )
    db_session.add(
        StoryArcPlacement(
            issue_story_arc_id=membership.id,
            placement_path="/arcs/Court of Owls/001.cbz",
            mode=StoryArcPlacementMode.COPY,
            ownership=StoryArcPlacementOwnership.MANAGED,
        )
    )
    await db_session.flush()

    with pytest.raises(StoryArcValidationError, match="managed placement"):
        await service.update(
            db_session,
            arc.id,
            expected_revision=2,
            name="The Court of Owls",
        )
    with pytest.raises(StoryArcValidationError, match="managed placement"):
        await service.update_membership(db_session, membership.id, sequence_number=2)
    with pytest.raises(StoryArcValidationError, match="managed placement"):
        await service.reorder_memberships(
            db_session,
            arc.id,
            ordered_membership_ids=[membership.id],
            expected_revision=2,
        )
    with pytest.raises(StoryArcValidationError, match="managed placement"):
        await service.remove_membership(db_session, membership.id)


def test_story_arc_service_has_no_provider_dependency() -> None:
    service_path = (
        Path(__file__).parents[2] / "src" / "pullbox" / "services" / "story_arc_service.py"
    )
    tree = ast.parse(service_path.read_text(encoding="utf-8"))
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )

    assert not any(module.startswith("pullbox.providers") for module in imported_modules)
