"""ORM contract tests for first-class story arcs and import staging."""

from __future__ import annotations

from itertools import product

import pytest
from sqlalchemy import CheckConstraint, inspect
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.exc import IntegrityError

from pullbox.models.base import UTCDateTime
from pullbox.models.import_job import ImportedFile, ImportJob, ImportJobAction
from pullbox.models.issue import Issue
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    ImportedStoryArcStatus,
    IssueStoryArc,
    StoryArc,
    StoryArcExternalIdentity,
    StoryArcLifecycle,
    StoryArcPlacement,
    StoryArcPlacementMode,
    StoryArcPlacementOwnership,
    StoryArcPlacementState,
    StoryArcResolutionState,
    StoryArcSourceKind,
    StoryArcSymlinkStyle,
)
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.models.story_arc_sync import StoryArcSyncWork

_VALID_PLACEMENT_COMBINATIONS = {
    (
        StoryArcPlacementMode.REFERENCE_ONLY,
        StoryArcPlacementOwnership.REFERENCED,
        None,
    ),
    (StoryArcPlacementMode.COPY, StoryArcPlacementOwnership.MANAGED, None),
    (StoryArcPlacementMode.HARDLINK, StoryArcPlacementOwnership.MANAGED, None),
    (
        StoryArcPlacementMode.SYMLINK,
        StoryArcPlacementOwnership.MANAGED,
        StoryArcSymlinkStyle.ABSOLUTE,
    ),
    (
        StoryArcPlacementMode.SYMLINK,
        StoryArcPlacementOwnership.MANAGED,
        StoryArcSymlinkStyle.RELATIVE,
    ),
}
_INVALID_PLACEMENT_COMBINATIONS = tuple(
    combination
    for combination in product(
        tuple(StoryArcPlacementMode),
        tuple(StoryArcPlacementOwnership),
        (None, *tuple(StoryArcSymlinkStyle)),
    )
    if combination not in _VALID_PLACEMENT_COMBINATIONS
)


def _column_names(model: type[object]) -> set[str]:
    return {column.name for column in model.__table__.columns}  # type: ignore[attr-defined]


def _relationship_names(model: type[object]) -> set[str]:
    return {relationship.key for relationship in inspect(model).relationships}


def _assert_lowercase_non_native_enum(model: type[object], column_name: str) -> None:
    column = model.__table__.c[column_name]  # type: ignore[attr-defined]
    assert isinstance(column.type, SQLAlchemyEnum)
    assert column.type.native_enum is False
    assert column.type.create_constraint is True
    assert column.type.enums == [value.lower() for value in column.type.enums]


def test_story_arc_enums_freeze_domain_values() -> None:
    assert {member.value for member in StoryArcSourceKind} == {
        "legacy",
        "pullbox",
        "mylar3",
        "folder",
        "comicinfo",
        "provider",
    }
    assert {member.value for member in StoryArcLifecycle} == {"active", "archived"}
    assert {member.value for member in StoryArcResolutionState} == {
        "pending",
        "resolved",
        "missing",
        "ambiguous",
        "conflict",
        "skipped",
    }
    assert {member.value for member in ImportedStoryArcStatus} == {
        "detected",
        "needs_review",
        "ready",
        "confirmed",
        "skipped",
        "imported",
        "failed",
    }
    assert {member.value for member in StoryArcPlacementMode} == {
        "copy",
        "hardlink",
        "symlink",
        "reference_only",
    }
    assert {member.value for member in StoryArcSymlinkStyle} == {"absolute", "relative"}
    assert {member.value for member in StoryArcPlacementOwnership} == {
        "managed",
        "referenced",
    }
    assert {member.value for member in StoryArcPlacementState} == {
        "current",
        "missing",
        "drifted",
        "failed",
    }


def test_story_arc_has_policy_monitoring_and_provenance_columns() -> None:
    assert {
        "normalized_name",
        "source_kind",
        "lifecycle",
        "monitored",
        "search_missing",
        "include_upcoming",
        "sync_enabled",
        "target_library_root_id",
        "policy_schema_version",
        "policy_snapshot",
        "source_import_job_id",
        "revision",
        "diagnostics",
    } <= _column_names(StoryArc)
    assert "deleted_at" not in _column_names(StoryArc)

    columns = StoryArc.__table__.c
    assert columns.normalized_name.nullable is False
    assert str(columns.normalized_name.server_default.arg) == "__legacy__"
    assert columns.monitored.default.arg is False
    assert columns.search_missing.default.arg is False
    assert columns.include_upcoming.default.arg is False
    assert columns.sync_enabled.default.arg is False
    assert columns.policy_schema_version.nullable is True
    assert columns.revision.default.arg == 1
    assert next(iter(columns.target_library_root_id.foreign_keys)).ondelete == "SET NULL"
    assert next(iter(columns.source_import_job_id.foreign_keys)).ondelete == "SET NULL"

    for column_name in ("source_kind", "lifecycle"):
        _assert_lowercase_non_native_enum(StoryArc, column_name)

    indexes = {index.name for index in StoryArc.__table__.indexes}
    assert {
        "ix_story_arcs_normalized_id",
        "ix_story_arcs_lifecycle_monitored_id",
        "ix_story_arcs_source_job_id",
    } <= indexes


def test_story_arc_name_dual_writes_conservative_normalized_identity() -> None:
    arc = StoryArc(name="  The  Court of Owls  ")

    assert arc.normalized_name == "the court of owls"
    arc.name = "Batman: Endgame"
    assert arc.normalized_name == "batman: endgame"


def test_membership_is_writable_ordered_entity_with_nullable_canonical_issue() -> None:
    assert IssueStoryArc.__table__.c.id.primary_key
    assert {
        "created_at",
        "updated_at",
        "issue_id",
        "story_arc_id",
        "sequence_number",
        "source_ordinal",
        "legacy_sequence_was_null",
        "resolution_state",
        "source_kind",
        "source_entry_id",
        "source_arc_id",
        "source_issue_id",
        "source_series_id",
        "source_issue_number_text",
        "source_series_name",
        "source_issue_title",
        "source_publisher",
        "source_release_date_text",
        "source_issue_date_text",
        "resolution_confidence",
        "resolution_method",
        "evidence",
        "sync_eligible",
        "last_materialization_result",
    } <= _column_names(IssueStoryArc)

    columns = IssueStoryArc.__table__.c
    assert columns.issue_id.nullable is True
    assert next(iter(columns.issue_id.foreign_keys)).ondelete == "SET NULL"
    assert columns.story_arc_id.nullable is False
    assert next(iter(columns.story_arc_id.foreign_keys)).ondelete == "CASCADE"
    assert columns.sequence_number.nullable is False
    assert columns.source_ordinal.nullable is False
    assert columns.resolution_state.default.arg is StoryArcResolutionState.PENDING
    assert columns.sync_eligible.default.arg is False
    _assert_lowercase_non_native_enum(IssueStoryArc, "resolution_state")
    _assert_lowercase_non_native_enum(IssueStoryArc, "source_kind")

    constraints = {constraint.name for constraint in IssueStoryArc.__table__.constraints}
    indexes = {index.name for index in IssueStoryArc.__table__.indexes}
    assert "uq_issue_story_arcs_arc_issue" in constraints
    assert {
        "ix_issue_story_arcs_order",
        "ix_issue_story_arcs_review",
        "ix_issue_story_arcs_issue",
    } <= indexes
    assert not any(
        "resolution_state" in str(constraint.sqltext).lower()
        and "issue_id" in str(constraint.sqltext).lower()
        for constraint in IssueStoryArc.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    )


def test_story_arc_relationships_keep_viewonly_compatibility_and_writable_memberships() -> None:
    story_arc_relationships = inspect(StoryArc).relationships
    issue_relationships = inspect(Issue).relationships

    assert story_arc_relationships.memberships.viewonly is False
    assert story_arc_relationships.memberships.cascade.delete_orphan
    assert story_arc_relationships.issues.viewonly is True
    assert issue_relationships.story_arc_memberships.viewonly is False
    assert issue_relationships.story_arcs.viewonly is True
    assert {"story_arc", "issue", "placements"} <= _relationship_names(IssueStoryArc)


def test_external_identity_is_provider_neutral_and_scoped_globally() -> None:
    assert StoryArcExternalIdentity.__tablename__ == "story_arc_external_identities"
    assert {
        "story_arc_id",
        "source",
        "namespace",
        "external_id",
        "source_url",
        "evidence",
    } <= _column_names(StoryArcExternalIdentity)
    constraints = {constraint.name for constraint in StoryArcExternalIdentity.__table__.constraints}
    assert "uq_story_arc_external_identity" in constraints


def test_import_staging_is_separate_from_final_arc_and_membership_rows() -> None:
    assert ImportedStoryArc.__tablename__ == "import_story_arcs"
    assert ImportedStoryArcEntry.__tablename__ == "import_story_arc_entries"
    assert {
        "import_job_id",
        "source_kind",
        "source_key",
        "source_arc_id",
        "source_ordinal",
        "name",
        "normalized_name",
        "description",
        "status",
        "selected_for_import",
        "proposed_story_arc_id",
        "materialized_story_arc_id",
        "proposed_policy_snapshot",
        "source_settings_snapshot",
        "diagnostics",
    } <= _column_names(ImportedStoryArc)
    assert {
        "imported_story_arc_id",
        "import_file_id",
        "matched_issue_id",
        "materialized_membership_id",
        "source_ordinal",
        "reading_order",
        "reading_order_raw",
        "resolution_state",
        "source_kind",
        "source_entry_id",
        "source_arc_id",
        "source_issue_id",
        "source_series_id",
        "source_issue_number_text",
        "source_series_name",
        "source_issue_title",
        "source_publisher",
        "source_release_date_text",
        "source_issue_date_text",
        "resolution_confidence",
        "resolution_method",
        "evidence",
        "source_location",
        "selected_for_import",
        "diagnostics",
    } <= _column_names(ImportedStoryArcEntry)

    imported_arc_relationships = inspect(ImportedStoryArc).relationships
    assert imported_arc_relationships.proposed_story_arc._calculated_foreign_keys == {
        ImportedStoryArc.__table__.c.proposed_story_arc_id
    }
    assert imported_arc_relationships.materialized_story_arc._calculated_foreign_keys == {
        ImportedStoryArc.__table__.c.materialized_story_arc_id
    }
    assert imported_arc_relationships.entries.cascade.delete_orphan

    imported_entry_relationships = inspect(ImportedStoryArcEntry).relationships
    assert imported_entry_relationships.matched_issue._calculated_foreign_keys == {
        ImportedStoryArcEntry.__table__.c.matched_issue_id
    }
    assert imported_entry_relationships.materialized_membership._calculated_foreign_keys == {
        ImportedStoryArcEntry.__table__.c.materialized_membership_id
    }
    assert "story_arcs" in _relationship_names(ImportJob)

    imported_arc_constraints = {
        constraint.name for constraint in ImportedStoryArc.__table__.constraints
    }
    imported_entry_constraints = {
        constraint.name for constraint in ImportedStoryArcEntry.__table__.constraints
    }
    assert "uq_import_story_arcs_job_source_key" in imported_arc_constraints
    assert "uq_import_story_arc_entries_arc_ordinal" in imported_entry_constraints
    assert ImportedStoryArc.__table__.c.source_ordinal.nullable is False
    assert ImportedStoryArc.__table__.c.name.nullable is True
    assert ImportedStoryArc.__table__.c.normalized_name.nullable is True
    imported_entry_indexes = {
        index.name: tuple(index.columns.keys()) for index in ImportedStoryArcEntry.__table__.indexes
    }
    assert imported_entry_indexes["ix_import_story_arc_entries_import_file_id"] == (
        "import_file_id",
    )


def test_imported_files_capture_folder_cohort_order_without_reorganizing_source() -> None:
    columns = ImportedFile.__table__.c
    assert columns.source_folder_cohort_key.type.length == 1000
    assert columns.source_folder_cohort_key.nullable is True
    assert columns.source_ordinal.nullable is True
    indexes = {index.name for index in ImportedFile.__table__.indexes}
    assert "ix_import_files_job_cohort_order" in indexes


def test_iu6b_placement_schema_freezes_managed_mode_constraints_and_audit_fields() -> None:
    assert StoryArcPlacement.__tablename__ == "story_arc_placements"
    assert {
        "issue_story_arc_id",
        "library_file_id",
        "library_root_id",
        "placement_path",
        "mode",
        "symlink_style",
        "ownership",
        "source_kind",
        "source_import_job_id",
        "creating_action_id",
        "rendered_reading_order",
        "policy_schema_version",
        "operation_token",
        "source_fingerprint",
        "state",
        "last_result",
        "last_checked_at",
    } <= _column_names(StoryArcPlacement)

    for column_name in (
        "mode",
        "symlink_style",
        "ownership",
        "source_kind",
        "state",
    ):
        _assert_lowercase_non_native_enum(StoryArcPlacement, column_name)

    constraints = {constraint.name for constraint in StoryArcPlacement.__table__.constraints}
    indexes = {index.name for index in StoryArcPlacement.__table__.indexes}
    assert "uq_story_arc_placements_path" in constraints
    assert {
        "ck_story_arc_placements_mode_ownership",
        "ck_story_arc_placements_symlink_style",
    } <= constraints
    assert (
        not {
            "ck_story_arc_placements_reference_only_mode",
            "ck_story_arc_placements_reference_only_owner",
            "ck_story_arc_placements_no_symlink_style",
        }
        & constraints
    )
    assert {
        "ix_story_arc_placements_membership",
        "ix_story_arc_placements_library_file",
        "ix_story_arc_placements_state",
    } <= indexes

    StoryArcPlacement(
        issue_story_arc_id=1,
        placement_path="arc/001.cbz",
        mode=StoryArcPlacementMode.REFERENCE_ONLY,
        ownership=StoryArcPlacementOwnership.REFERENCED,
        symlink_style=None,
    ).validate_configuration()


def test_import_owned_sync_work_model_preserves_action_provenance_and_cancellation() -> None:
    """Import-triggered sync work is uniquely attributable and durably cancellable."""
    columns = StoryArcSyncWork.__table__.c
    assert columns.origin_import_action_id.nullable is True
    assert columns.origin_import_job_id.nullable is True
    assert columns.origin_imported_story_arc_id.nullable is True
    assert columns.origin_imported_story_arc_entry_id.nullable is True
    assert columns.cancel_requested_at.nullable is True
    assert isinstance(columns.cancel_requested_at.type, UTCDateTime)

    origin_foreign_key = next(
        foreign_key
        for foreign_key in StoryArcSyncWork.__table__.foreign_key_constraints
        if tuple(foreign_key.column_keys) == ("origin_import_action_id",)
    )
    assert origin_foreign_key.referred_table is ImportJobAction.__table__
    assert origin_foreign_key.ondelete == "SET NULL"
    typed_origin_foreign_keys = {
        tuple(foreign_key.column_keys): foreign_key
        for foreign_key in StoryArcSyncWork.__table__.foreign_key_constraints
    }
    assert (
        typed_origin_foreign_keys[("origin_import_job_id",)].referred_table is ImportJob.__table__
    )
    assert typed_origin_foreign_keys[("origin_import_job_id",)].ondelete == "SET NULL"
    assert (
        typed_origin_foreign_keys[("origin_imported_story_arc_id",)].referred_table
        is ImportedStoryArc.__table__
    )
    assert typed_origin_foreign_keys[("origin_imported_story_arc_id",)].ondelete == "SET NULL"
    assert (
        typed_origin_foreign_keys[("origin_imported_story_arc_entry_id",)].referred_table
        is ImportedStoryArcEntry.__table__
    )
    assert typed_origin_foreign_keys[("origin_imported_story_arc_entry_id",)].ondelete == "SET NULL"

    constraints = {constraint.name for constraint in StoryArcSyncWork.__table__.constraints}
    assert "uq_story_arc_sync_work_origin_import_action" in constraints
    relationships = inspect(StoryArcSyncWork).relationships
    assert relationships.origin_import_action._calculated_foreign_keys == {
        columns.origin_import_action_id
    }

    sync_indexes = {
        index.name: tuple(index.columns.keys()) for index in StoryArcSyncWork.__table__.indexes
    }
    assert sync_indexes["ix_story_arc_sync_work_queued"] == (
        "claimable",
        "state",
        "created_at",
        "id",
    )
    assert sync_indexes["ix_story_arc_sync_work_ready"] == (
        "claimable",
        "state",
        "next_attempt_at",
        "id",
    )
    assert sync_indexes["ix_story_arc_sync_work_stale_claim"] == (
        "claimable",
        "state",
        "claimed_at",
        "id",
    )
    assert sync_indexes["ix_story_arc_sync_work_origin_job_state"] == (
        "origin_import_job_id",
        "state",
        "id",
    )

    import_job_columns = ImportJob.__table__.c
    assert import_job_columns.story_arc_placement_followup_pending.nullable is False
    assert import_job_columns.story_arc_placement_followup_pending.default.arg is False
    assert import_job_columns.story_arc_rollback_waiting_work_id.nullable is True
    rollback_foreign_key = next(
        foreign_key
        for foreign_key in ImportJob.__table__.foreign_key_constraints
        if tuple(foreign_key.column_keys) == ("story_arc_rollback_waiting_work_id",)
    )
    assert rollback_foreign_key.referred_table is StoryArcSyncWork.__table__
    assert rollback_foreign_key.ondelete == "SET NULL"
    import_job_indexes = {
        index.name: tuple(index.columns.keys()) for index in ImportJob.__table__.indexes
    }
    assert import_job_indexes["ix_import_jobs_story_arc_followup"] == (
        "status",
        "story_arc_placement_followup_pending",
        "id",
    )
    assert import_job_indexes["ix_import_jobs_story_arc_rollback_waiting"] == (
        "status",
        "story_arc_rollback_waiting_work_id",
        "id",
    )
    action_indexes = {
        index.name: tuple(index.columns.keys()) for index in ImportJobAction.__table__.indexes
    }
    assert action_indexes["ix_import_job_actions_job_id_keyset"] == (
        "import_job_id",
        "id",
    )

    placement_indexes = {
        index.name: tuple(index.columns.keys()) for index in StoryArcPlacement.__table__.indexes
    }
    assert placement_indexes["ix_story_arc_placements_creating_action"] == (
        "creating_action_id",
        "id",
    )


@pytest.mark.parametrize(
    ("mode", "ownership", "symlink_style"),
    sorted(
        _VALID_PLACEMENT_COMBINATIONS,
        key=lambda item: (item[0].value, item[1].value, str(item[2])),
    ),
)
def test_iu6b_placement_validation_accepts_every_valid_combination_in_any_kwarg_order(
    mode: StoryArcPlacementMode,
    ownership: StoryArcPlacementOwnership,
    symlink_style: StoryArcSymlinkStyle | None,
) -> None:
    forward = StoryArcPlacement(
        issue_story_arc_id=1,
        placement_path=f"arc/forward-{mode.value}-{symlink_style}.cbz",
        mode=mode,
        ownership=ownership,
        symlink_style=symlink_style,
    )
    reverse_kwargs = {
        "symlink_style": symlink_style,
        "ownership": ownership,
        "mode": mode,
        "placement_path": f"arc/reverse-{mode.value}-{symlink_style}.cbz",
        "issue_story_arc_id": 1,
    }
    reverse = StoryArcPlacement(**reverse_kwargs)

    forward.validate_configuration()
    reverse.validate_configuration()


@pytest.mark.parametrize(
    ("mode", "ownership", "symlink_style"),
    _INVALID_PLACEMENT_COMBINATIONS,
)
def test_iu6b_placement_validation_rejects_every_invalid_combination(
    mode: StoryArcPlacementMode,
    ownership: StoryArcPlacementOwnership,
    symlink_style: StoryArcSymlinkStyle | None,
) -> None:
    placement = StoryArcPlacement(
        issue_story_arc_id=1,
        placement_path=f"arc/invalid-{mode.value}-{ownership.value}-{symlink_style}.cbz",
        mode=mode,
        ownership=ownership,
        symlink_style=symlink_style,
    )

    with pytest.raises(ValueError, match="placement"):
        placement.validate_configuration()


@pytest.mark.asyncio
async def test_iu6b_create_all_schema_enforces_managed_mode_matrix(db_session) -> None:
    """Base.metadata.create_all carries the authoritative combination checks."""
    series = Series(title="Placement Matrix", sort_title="placement matrix")
    issue = Issue(series=series, issue_number=1)
    arc = StoryArc(name="Placement Matrix")
    membership = IssueStoryArc(
        story_arc=arc,
        issue=issue,
        sequence_number=1,
        source_ordinal=1,
        resolution_state=StoryArcResolutionState.RESOLVED,
    )
    db_session.add_all([series, issue, arc, membership])
    await db_session.flush()

    await db_session.execute(
        StoryArcPlacement.__table__.insert().values(
            issue_story_arc_id=membership.id,
            placement_path="arc/managed-copy.cbz",
            mode=StoryArcPlacementMode.COPY,
            ownership=StoryArcPlacementOwnership.MANAGED,
            symlink_style=None,
        )
    )
    orm_symlink = StoryArcPlacement(
        symlink_style=StoryArcSymlinkStyle.RELATIVE,
        ownership=StoryArcPlacementOwnership.MANAGED,
        mode=StoryArcPlacementMode.SYMLINK,
        placement_path="arc/managed-relative-symlink.cbz",
        issue_story_arc_id=membership.id,
    )
    db_session.add(orm_symlink)
    await db_session.flush()

    savepoint = await db_session.begin_nested()
    with pytest.raises(IntegrityError):
        await db_session.execute(
            StoryArcPlacement.__table__.insert().values(
                issue_story_arc_id=membership.id,
                placement_path="arc/invalid-referenced-copy.cbz",
                mode=StoryArcPlacementMode.COPY,
                ownership=StoryArcPlacementOwnership.REFERENCED,
                symlink_style=None,
            )
        )
    await savepoint.rollback()

    invalid_orm = StoryArcPlacement(
        issue_story_arc_id=membership.id,
        placement_path="arc/invalid-orm-copy.cbz",
        mode=StoryArcPlacementMode.COPY,
        ownership=StoryArcPlacementOwnership.REFERENCED,
    )
    db_session.add(invalid_orm)
    with pytest.raises(ValueError, match="requires managed ownership"):
        await db_session.flush()
