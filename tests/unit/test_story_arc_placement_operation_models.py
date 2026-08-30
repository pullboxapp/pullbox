"""Durable model contracts for arc-wide placement operations."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable

from pullbox.models.story_arc import (
    StoryArc,
    StoryArcPlacementMode,
    StoryArcPlacementOperationKind,
    StoryArcPlacementOwnership,
)
from pullbox.models.story_arc_placement_operation import (
    StoryArcPlacementOperation,
    StoryArcPlacementOperationAction,
    StoryArcPlacementOperationActionKind,
    StoryArcPlacementOperationActionState,
    StoryArcPlacementOperationRecoveryMode,
    StoryArcPlacementOperationState,
)


def _operation(*, story_arc_id: int, token: str) -> StoryArcPlacementOperation:
    return StoryArcPlacementOperation(
        story_arc_id=story_arc_id,
        operation_kind=StoryArcPlacementOperationKind.POLICY_MIGRATION,
        state=StoryArcPlacementOperationState.READY,
        active_fence_slot=1,
        operation_token=token,
        idempotency_key_hash=token * 2,
        expected_story_arc_revision=1,
        confirmation_schema_version=1,
        confirmed_at=datetime.now(UTC),
        source_policy_snapshot={"schema_version": 1, "mode": "copy"},
        proposed_policy_snapshot={"schema_version": 1, "mode": "hardlink"},
        source_library_root_id=101,
        target_library_root_id=102,
        source_root_fingerprint={"device": 1, "inode": 2},
        target_root_fingerprint={"device": 1, "inode": 3},
        scope_digest="a" * 64,
        plan_digest="b" * 64,
        recovery_mode=StoryArcPlacementOperationRecoveryMode.ROLLBACK,
        total_action_count=1,
        completed_action_count=0,
        failed_action_count=0,
        required_bytes=123,
        attempt_count=0,
        last_result={},
    )


def _action(*, operation_id: int, sequence_number: int = 1) -> StoryArcPlacementOperationAction:
    return StoryArcPlacementOperationAction(
        operation_id=operation_id,
        sequence_number=sequence_number,
        action_kind=StoryArcPlacementOperationActionKind.MIGRATE_MANAGED,
        state=StoryArcPlacementOperationActionState.PLANNED,
        source_placement_id=987_654,
        shadow_placement_id=None,
        membership_id=123_456,
        library_file_id=456_789,
        source_library_root_id=101,
        target_library_root_id=102,
        source_path="/private/library/arcs/001.cbz",
        target_path="/private/library/new-arcs/001.cbz",
        target_path_exact_hash="c" * 64,
        target_path_casefold_hash="d" * 64,
        temporary_path=None,
        rollback_path=None,
        source_mode=StoryArcPlacementMode.COPY,
        target_mode=StoryArcPlacementMode.HARDLINK,
        source_ownership=StoryArcPlacementOwnership.MANAGED,
        target_ownership=StoryArcPlacementOwnership.MANAGED,
        prior_placement_snapshot={"state": "current", "path": "private"},
        source_root_fingerprint={"device": 1, "inode": 2},
        target_root_fingerprint={"device": 1, "inode": 3},
        expected_source_fingerprint={"size": 123, "mtime_ns": 456},
        staged_target_fingerprint={},
        published_target_fingerprint={},
        required_bytes=123,
        attempt_count=0,
        last_result={},
    )


def test_operation_models_expose_normalized_coordinator_and_private_action_journal() -> None:
    operation_columns = {column.name for column in StoryArcPlacementOperation.__table__.columns}
    action_columns = {column.name for column in StoryArcPlacementOperationAction.__table__.columns}
    operation_relationships = {
        relationship.key for relationship in inspect(StoryArcPlacementOperation).relationships
    }
    action_relationships = {
        relationship.key for relationship in inspect(StoryArcPlacementOperationAction).relationships
    }

    assert {
        "story_arc_id",
        "actor_user_id",
        "operation_kind",
        "state",
        "active_fence_slot",
        "operation_token",
        "idempotency_key_hash",
        "expected_story_arc_revision",
        "confirmation_schema_version",
        "confirmed_at",
        "source_policy_snapshot",
        "proposed_policy_snapshot",
        "source_library_root_id",
        "target_library_root_id",
        "source_root_fingerprint",
        "target_root_fingerprint",
        "scope_digest",
        "plan_digest",
        "recovery_mode",
        "claim_token",
        "claimed_at",
        "claim_heartbeat_at",
        "claim_expires_at",
        "next_attempt_at",
        "cancel_requested_at",
        "policy_applied_at",
        "applied_policy_revision",
        "applied_policy_digest",
        "total_action_count",
        "completed_action_count",
        "failed_action_count",
        "required_bytes",
        "attempt_count",
        "started_at",
        "completed_at",
        "last_error_code",
        "last_error_category",
        "last_error_detail",
        "last_result",
    } <= operation_columns
    assert {
        "operation_id",
        "sequence_number",
        "action_kind",
        "state",
        "source_placement_id",
        "shadow_placement_id",
        "membership_id",
        "library_file_id",
        "source_library_root_id",
        "target_library_root_id",
        "source_path",
        "target_path",
        "target_path_exact_hash",
        "target_path_casefold_hash",
        "temporary_path",
        "rollback_path",
        "source_mode",
        "target_mode",
        "source_ownership",
        "target_ownership",
        "prior_placement_snapshot",
        "source_root_fingerprint",
        "target_root_fingerprint",
        "expected_source_fingerprint",
        "staged_target_fingerprint",
        "published_target_fingerprint",
        "required_bytes",
        "attempt_count",
        "claim_token",
        "claimed_at",
        "claim_heartbeat_at",
        "claim_expires_at",
        "next_attempt_at",
        "cancel_observed_at",
        "recovery_required_at",
        "reserved_at",
        "staged_at",
        "published_at",
        "verified_at",
        "old_removed_at",
        "db_reconciled_at",
        "cleaned_at",
        "rolled_back_at",
        "last_error_code",
        "last_error_category",
        "last_error_detail",
        "last_result",
    } <= action_columns
    assert operation_relationships == {"story_arc", "actor_user", "actions"}
    assert action_relationships == {"operation"}
    assert "operation_progress_id" not in operation_columns
    assert "operation_progress_id" not in action_columns


def test_journal_identity_columns_are_not_foreign_keys_to_mutable_placement_rows() -> None:
    action_foreign_keys = {
        (foreign_key.parent.name, foreign_key.target_fullname)
        for foreign_key in StoryArcPlacementOperationAction.__table__.foreign_keys
    }

    assert action_foreign_keys == {("operation_id", "story_arc_placement_operations.id")}
    assert all("operation_progress" not in target for _, target in action_foreign_keys)

    story_arc_foreign_key = next(
        foreign_key
        for foreign_key in StoryArcPlacementOperation.__table__.foreign_keys
        if foreign_key.parent.name == "story_arc_id"
    )
    placement_operations = inspect(StoryArc).relationships.placement_operations
    assert story_arc_foreign_key.ondelete == "RESTRICT"
    assert "delete" not in placement_operations.cascade
    assert "delete-orphan" not in placement_operations.cascade


def test_action_journal_contains_only_managed_filesystem_mutations() -> None:
    assert set(StoryArcPlacementOperationActionKind) == {
        StoryArcPlacementOperationActionKind.MIGRATE_MANAGED,
        StoryArcPlacementOperationActionKind.REBUILD_MANAGED,
        StoryArcPlacementOperationActionKind.REMOVE_MANAGED,
    }


def test_coordinator_records_each_crash_recovery_boundary() -> None:
    assert set(StoryArcPlacementOperationState) == {
        StoryArcPlacementOperationState.BUILDING,
        StoryArcPlacementOperationState.READY,
        StoryArcPlacementOperationState.RUNNING,
        StoryArcPlacementOperationState.RECONCILING,
        StoryArcPlacementOperationState.CLEANUP_PENDING,
        StoryArcPlacementOperationState.CANCEL_REQUESTED,
        StoryArcPlacementOperationState.ROLLING_BACK,
        StoryArcPlacementOperationState.RECOVERY_REQUIRED,
        StoryArcPlacementOperationState.COMPLETED,
        StoryArcPlacementOperationState.CANCELLED,
        StoryArcPlacementOperationState.FAILED,
    }
    assert set(StoryArcPlacementOperationActionState) == {
        StoryArcPlacementOperationActionState.PLANNED,
        StoryArcPlacementOperationActionState.RESERVED,
        StoryArcPlacementOperationActionState.STAGED,
        StoryArcPlacementOperationActionState.PUBLISHED,
        StoryArcPlacementOperationActionState.VERIFIED,
        StoryArcPlacementOperationActionState.OLD_REMOVED,
        StoryArcPlacementOperationActionState.DB_RECONCILED,
        StoryArcPlacementOperationActionState.CLEANUP_PENDING,
        StoryArcPlacementOperationActionState.CLEANED,
        StoryArcPlacementOperationActionState.ROLLING_BACK,
        StoryArcPlacementOperationActionState.ROLLED_BACK,
        StoryArcPlacementOperationActionState.RECOVERY_REQUIRED,
        StoryArcPlacementOperationActionState.FAILED,
    }


def test_operation_tables_compile_for_sqlite_and_postgresql() -> None:
    for dialect in (sqlite.dialect(), postgresql.dialect()):
        operation_sql = str(
            CreateTable(StoryArcPlacementOperation.__table__).compile(dialect=dialect)
        )
        action_sql = str(
            CreateTable(StoryArcPlacementOperationAction.__table__).compile(dialect=dialect)
        )

        assert "story_arc_placement_operations" in operation_sql
        assert "story_arc_placement_operation_actions" in action_sql
        assert "idempotency_key_hash" in operation_sql
        assert "source_placement_id" in action_sql
        assert "story_arc_placements" not in action_sql
        assert "operation_progress" not in operation_sql
        assert "operation_progress" not in action_sql


async def test_active_fence_is_owned_by_arc_and_token_is_globally_unique(db_session) -> None:
    first_arc = StoryArc(
        name="Operation fence one",
        active_placement_operation_token="1" * 32,
        active_placement_operation_kind=StoryArcPlacementOperationKind.POLICY_MIGRATION,
    )
    db_session.add(first_arc)
    await db_session.flush()

    db_session.add(
        StoryArc(
            name="Operation fence two",
            active_placement_operation_token="1" * 32,
            active_placement_operation_kind=StoryArcPlacementOperationKind.REORDER,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    db_session.add(
        StoryArc(
            name="Incomplete operation fence",
            active_placement_operation_token="2" * 32,
            active_placement_operation_kind=None,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_policy_boundary_requires_rollback_before_apply_and_forward_only_after(
    db_session,
) -> None:
    arc = StoryArc(name="Policy boundary")
    db_session.add(arc)
    await db_session.flush()

    operation = _operation(story_arc_id=arc.id, token="3" * 32)
    operation.state = StoryArcPlacementOperationState.RUNNING
    operation.recovery_mode = StoryArcPlacementOperationRecoveryMode.FORWARD_ONLY
    db_session.add(operation)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    arc = StoryArc(name="Policy boundary valid")
    db_session.add(arc)
    await db_session.flush()
    operation = _operation(story_arc_id=arc.id, token="6" * 32)
    operation.state = StoryArcPlacementOperationState.RUNNING
    operation.recovery_mode = StoryArcPlacementOperationRecoveryMode.FORWARD_ONLY
    operation.policy_applied_at = datetime.now(UTC)
    operation.applied_policy_revision = 2
    operation.applied_policy_digest = "e" * 64
    db_session.add(operation)
    await db_session.flush()
    assert operation.recovery_mode is StoryArcPlacementOperationRecoveryMode.FORWARD_ONLY

    rollback_arc = StoryArc(name="Rollback boundary invalid")
    db_session.add(rollback_arc)
    await db_session.flush()
    rollback_with_applied_evidence = _operation(
        story_arc_id=rollback_arc.id,
        token="7" * 32,
    )
    rollback_with_applied_evidence.policy_applied_at = datetime.now(UTC)
    rollback_with_applied_evidence.applied_policy_revision = 2
    rollback_with_applied_evidence.applied_policy_digest = "f" * 64
    db_session.add(rollback_with_applied_evidence)
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_action_sequence_is_unique_and_old_placement_identity_survives_without_parent(
    db_session,
) -> None:
    arc = StoryArc(name="Action journal")
    db_session.add(arc)
    await db_session.flush()
    operation = _operation(story_arc_id=arc.id, token="4" * 32)
    db_session.add(operation)
    await db_session.flush()

    first = _action(operation_id=operation.id)
    db_session.add(first)
    await db_session.flush()
    assert first.source_placement_id == 987_654

    duplicate_sequence = _action(operation_id=operation.id)
    duplicate_sequence.source_placement_id += 1
    duplicate_sequence.target_path_exact_hash = "e" * 64
    duplicate_sequence.target_path_casefold_hash = "f" * 64
    db_session.add(duplicate_sequence)
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_target_exact_and_casefold_hashes_are_unique_per_operation(db_session) -> None:
    arc = StoryArc(name="Target uniqueness")
    db_session.add(arc)
    await db_session.flush()
    operation = _operation(story_arc_id=arc.id, token="8" * 32)
    operation.total_action_count = 2
    db_session.add(operation)
    await db_session.flush()

    first = _action(operation_id=operation.id, sequence_number=1)
    db_session.add(first)
    await db_session.flush()
    duplicate_casefold = _action(operation_id=operation.id, sequence_number=2)
    duplicate_casefold.source_placement_id += 1
    duplicate_casefold.target_path_exact_hash = "e" * 64
    db_session.add(duplicate_casefold)
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_source_placement_is_journaled_once_per_operation(db_session) -> None:
    arc = StoryArc(name="Source uniqueness")
    db_session.add(arc)
    await db_session.flush()
    operation = _operation(story_arc_id=arc.id, token="9" * 32)
    operation.total_action_count = 2
    db_session.add(operation)
    await db_session.flush()

    db_session.add(_action(operation_id=operation.id, sequence_number=1))
    await db_session.flush()
    duplicate_source = _action(operation_id=operation.id, sequence_number=2)
    duplicate_source.target_path_exact_hash = "e" * 64
    duplicate_source.target_path_casefold_hash = "f" * 64
    db_session.add(duplicate_source)
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_idempotency_identity_is_scoped_to_arc_and_operation_kind(db_session) -> None:
    first_arc = StoryArc(name="Idempotency one")
    second_arc = StoryArc(name="Idempotency two")
    db_session.add_all([first_arc, second_arc])
    await db_session.flush()

    first = _operation(story_arc_id=first_arc.id, token="a" * 32)
    shared_hash = "1" * 64
    first.idempotency_key_hash = shared_hash
    db_session.add(first)
    await db_session.flush()
    first.state = StoryArcPlacementOperationState.COMPLETED
    first.active_fence_slot = None
    first.completed_at = datetime.now(UTC)
    await db_session.flush()

    different_arc = _operation(story_arc_id=second_arc.id, token="b" * 32)
    different_arc.idempotency_key_hash = shared_hash
    different_kind = _operation(story_arc_id=first_arc.id, token="c" * 32)
    different_kind.idempotency_key_hash = shared_hash
    different_kind.operation_kind = StoryArcPlacementOperationKind.REORDER
    db_session.add_all([different_arc, different_kind])
    await db_session.flush()
    completed_at = datetime.now(UTC)
    for operation in (different_arc, different_kind):
        operation.state = StoryArcPlacementOperationState.COMPLETED
        operation.active_fence_slot = None
        operation.completed_at = completed_at
    await db_session.flush()

    duplicate_scope = _operation(story_arc_id=first_arc.id, token="d" * 32)
    duplicate_scope.idempotency_key_hash = shared_hash
    db_session.add(duplicate_scope)
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_coordinator_active_slot_rejects_a_stray_second_active_row(db_session) -> None:
    arc = StoryArc(name="Coordinator fence")
    db_session.add(arc)
    await db_session.flush()
    first = _operation(story_arc_id=arc.id, token="e" * 32)
    db_session.add(first)
    await db_session.flush()

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(_operation(story_arc_id=arc.id, token="f" * 32))
            await db_session.flush()

    first.state = StoryArcPlacementOperationState.COMPLETED
    first.active_fence_slot = None
    first.completed_at = datetime.now(UTC)
    await db_session.flush()
    replacement = _operation(story_arc_id=arc.id, token="0" * 32)
    db_session.add(replacement)
    await db_session.flush()
    assert replacement.id != first.id
