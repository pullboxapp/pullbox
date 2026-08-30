"""Explicit Step 3 confirmation for staged story-arc policies."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pullbox.core.exceptions import ValidationError
from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType
from pullbox.models.library import LibraryRoot
from pullbox.models.story_arc import ImportedStoryArcStatus, StoryArcSourceKind
from pullbox.models.story_arc_import import ImportedStoryArc
from pullbox.services.import_story_arc_policy_confirmation import (
    build_import_story_arc_policy_review,
    confirm_import_story_arc_policy,
    story_arc_policy_digest,
)
from pullbox.services.story_arc_placement_integration import (
    StoryArcPlacementPolicyInput,
    StoryArcPlacementPolicyMode,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


def _draft() -> dict[str, object]:
    return {
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
            "destination_root": "/source/private/StoryArcs",
            "folder_template": "{StoryArc}",
            "file_template": "{ReadingOrder:03d} - {Series} {IssueNumber}",
            "symlink_style": None,
            "synchronize": True,
        },
        "confirmation": {
            "target_library_root_required": True,
            "destination_root_requires_approval": True,
            "ready_for_activation": False,
        },
    }


async def _stage(
    session: AsyncSession,
    *,
    status: ImportJobStatus = ImportJobStatus.REVIEW,
) -> tuple[ImportJob, ImportedStoryArc]:
    job = ImportJob(
        source_path="/source/private/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=status,
    )
    session.add(job)
    await session.flush()
    arc = ImportedStoryArc(
        import_job_id=job.id,
        source_kind=StoryArcSourceKind.MYLAR3,
        source_key="mylar3:policy",
        source_ordinal=1,
        name="Knightfall",
        status=ImportedStoryArcStatus.READY,
        selected_for_import=True,
        proposed_policy_snapshot=_draft(),
    )
    session.add(arc)
    await session.flush()
    return job, arc


def _logical_policy() -> StoryArcPlacementPolicyInput:
    return StoryArcPlacementPolicyInput(
        mode=StoryArcPlacementPolicyMode.LOGICAL,
        target_library_root_id=None,
        destination_root=None,
        folder_template="{StoryArc}",
        file_template="{Series} {IssueNumber}",
        symlink_style=None,
        synchronize=False,
    )


def test_policy_review_redacts_source_paths_and_renders_a_data_only_example() -> None:
    review = build_import_story_arc_policy_review(
        _draft(),
        {
            "parse_warnings": ["unknown_value:ARC_FILEOPS"],
            "values": {
                "STORYARC_LOCATION": {"value": "/private/source/StoryArcs"},
                "ARC_FILEOPS": {"value": "move"},
            },
        },
    )

    assert review.confirmed is False
    assert review.destination_configured is True
    assert review.warnings == (
        "legacy_move_mapped_to_copy",
        "unknown_value:ARC_FILEOPS",
    )
    assert review.source_settings[0].value == "Configured"
    assert review.source_settings[1].value == "move"
    assert review.example_relative_path == ("Example Arc/001 - Example Series 1.cbz")
    assert "/private/" not in repr(review)


@pytest.mark.asyncio
async def test_logical_policy_confirmation_persists_only_the_canonical_envelope(
    db_session: AsyncSession,
) -> None:
    job, arc = await _stage(db_session)
    original_digest = story_arc_policy_digest(arc.proposed_policy_snapshot)

    result = await confirm_import_story_arc_policy(
        db_session,
        job_id=job.id,
        imported_story_arc_id=arc.id,
        expected_policy_digest=original_digest,
        explicit_confirmation=True,
        materialize_filesystem=False,
        monitored=True,
        search_missing=True,
        include_upcoming=False,
        placement_policy=_logical_policy(),
    )

    assert arc.proposed_policy_snapshot == {
        "schema_version": 1,
        "source": "mylar3",
        "activation": "confirmed",
        "monitored": True,
        "search_missing": True,
        "include_upcoming": False,
        "sync_enabled": False,
        "placement_policy": {
            "schema_version": 1,
            "mode": "logical",
            "target_library_root_id": None,
            "destination_root": None,
            "folder_template": "{StoryArc}",
            "file_template": "{Series} {IssueNumber}",
            "symlink_style": None,
            "synchronize": False,
        },
    }
    assert result.policy_digest == story_arc_policy_digest(arc.proposed_policy_snapshot)
    assert result.materialize_filesystem is False
    assert arc.status == ImportedStoryArcStatus.READY
    assert arc.diagnostics["story_arc_policy_review"] == {
        "schema_version": 1,
        "warning_codes": ["legacy_move_mapped_to_copy"],
    }
    review = build_import_story_arc_policy_review(
        arc.proposed_policy_snapshot,
        arc.source_settings_snapshot,
        arc.diagnostics,
    )
    assert review.confirmed is True
    assert review.warnings == ("legacy_move_mapped_to_copy",)


@pytest.mark.asyncio
async def test_managed_policy_requires_and_freezes_an_enabled_root_and_existing_destination(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    job, arc = await _stage(db_session)
    library = tmp_path / "library"
    destination = tmp_path / "StoryArcs"
    library.mkdir()
    destination.mkdir()
    root = LibraryRoot(name="Comics", path=str(library), enabled=True)
    db_session.add(root)
    await db_session.flush()

    result = await confirm_import_story_arc_policy(
        db_session,
        job_id=job.id,
        imported_story_arc_id=arc.id,
        expected_policy_digest=story_arc_policy_digest(arc.proposed_policy_snapshot),
        explicit_confirmation=True,
        materialize_filesystem=True,
        monitored=True,
        search_missing=True,
        include_upcoming=True,
        placement_policy=StoryArcPlacementPolicyInput(
            mode=StoryArcPlacementPolicyMode.COPY,
            target_library_root_id=root.id,
            destination_root=str(destination),
            folder_template="{Publisher} - {StoryArc}",
            file_template="{ReadingOrder:03d} - {Series} {IssueNumber}",
            symlink_style=None,
            synchronize=True,
        ),
    )

    placement = arc.proposed_policy_snapshot["placement_policy"]
    assert placement["target_library_root_id"] == root.id
    assert placement["destination_root"] == str(destination.resolve())
    assert placement["mode"] == "copy"
    assert placement["synchronize"] is True
    assert arc.proposed_policy_snapshot["sync_enabled"] is True
    assert result.materialize_filesystem is True


@pytest.mark.asyncio
async def test_confirmation_requires_explicit_consent_and_current_draft_digest(
    db_session: AsyncSession,
) -> None:
    job, arc = await _stage(db_session)
    original = dict(arc.proposed_policy_snapshot)

    with pytest.raises(ValidationError, match="explicitly confirm"):
        await confirm_import_story_arc_policy(
            db_session,
            job_id=job.id,
            imported_story_arc_id=arc.id,
            expected_policy_digest=story_arc_policy_digest(original),
            explicit_confirmation=False,
            materialize_filesystem=False,
            monitored=False,
            search_missing=False,
            include_upcoming=False,
            placement_policy=_logical_policy(),
        )
    with pytest.raises(ValidationError, match="changed"):
        await confirm_import_story_arc_policy(
            db_session,
            job_id=job.id,
            imported_story_arc_id=arc.id,
            expected_policy_digest="0" * 64,
            explicit_confirmation=True,
            materialize_filesystem=False,
            monitored=False,
            search_missing=False,
            include_upcoming=False,
            placement_policy=_logical_policy(),
        )

    assert arc.proposed_policy_snapshot == original


@pytest.mark.asyncio
async def test_confirmation_rejects_non_review_jobs_and_materialization_shape_mismatch(
    db_session: AsyncSession,
) -> None:
    job, arc = await _stage(db_session, status=ImportJobStatus.IMPORTING)
    digest = story_arc_policy_digest(arc.proposed_policy_snapshot)

    with pytest.raises(ValidationError, match="REVIEW"):
        await confirm_import_story_arc_policy(
            db_session,
            job_id=job.id,
            imported_story_arc_id=arc.id,
            expected_policy_digest=digest,
            explicit_confirmation=True,
            materialize_filesystem=False,
            monitored=False,
            search_missing=False,
            include_upcoming=False,
            placement_policy=_logical_policy(),
        )

    job.status = ImportJobStatus.REVIEW
    with pytest.raises(ValidationError, match="materialization choice"):
        await confirm_import_story_arc_policy(
            db_session,
            job_id=job.id,
            imported_story_arc_id=arc.id,
            expected_policy_digest=digest,
            explicit_confirmation=True,
            materialize_filesystem=True,
            monitored=False,
            search_missing=False,
            include_upcoming=False,
            placement_policy=_logical_policy(),
        )


@pytest.mark.asyncio
async def test_confirmation_rejects_disabled_roots_without_mutating_the_draft(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    job, arc = await _stage(db_session)
    destination = tmp_path / "StoryArcs"
    destination.mkdir()
    root = LibraryRoot(name="Disabled", path=str(tmp_path), enabled=False)
    db_session.add(root)
    await db_session.flush()
    original = dict(arc.proposed_policy_snapshot)

    with pytest.raises(ValidationError, match="unavailable"):
        await confirm_import_story_arc_policy(
            db_session,
            job_id=job.id,
            imported_story_arc_id=arc.id,
            expected_policy_digest=story_arc_policy_digest(original),
            explicit_confirmation=True,
            materialize_filesystem=True,
            monitored=False,
            search_missing=False,
            include_upcoming=False,
            placement_policy=StoryArcPlacementPolicyInput(
                mode=StoryArcPlacementPolicyMode.COPY,
                target_library_root_id=root.id,
                destination_root=str(destination),
                folder_template="{StoryArc}",
                file_template="{Series} {IssueNumber}",
                symlink_style=None,
                synchronize=False,
            ),
        )

    assert arc.proposed_policy_snapshot == original


@pytest.mark.asyncio
async def test_confirmation_rejects_automation_without_monitoring(
    db_session: AsyncSession,
) -> None:
    job, arc = await _stage(db_session)
    original = dict(arc.proposed_policy_snapshot)

    with pytest.raises(ValidationError, match="monitoring"):
        await confirm_import_story_arc_policy(
            db_session,
            job_id=job.id,
            imported_story_arc_id=arc.id,
            expected_policy_digest=story_arc_policy_digest(original),
            explicit_confirmation=True,
            materialize_filesystem=False,
            monitored=False,
            search_missing=True,
            include_upcoming=False,
            placement_policy=_logical_policy(),
        )

    assert arc.proposed_policy_snapshot == original
    assert arc.diagnostics == {}
