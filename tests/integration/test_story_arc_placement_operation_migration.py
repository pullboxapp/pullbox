"""Alembic coverage for durable arc-wide placement-operation journals."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command

if TYPE_CHECKING:
    from collections.abc import Generator

_ALEMBIC_DIR = Path(__file__).resolve().parent.parent.parent / "alembic"
_PARENT_REVISION = "e6f7a8b9c012"


@pytest.fixture
def placement_operation_migration_config(
    tmp_path: Path,
) -> Generator[tuple[Config, str], None, None]:
    db_path = tmp_path / "story-arc-placement-operation-migration.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"
    config = Config(str(_ALEMBIC_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(_ALEMBIC_DIR))
    config.set_main_option("sqlalchemy.url", async_url)
    prior_url = os.environ.get("PULLBOX_DB_URL")
    os.environ["PULLBOX_DB_URL"] = async_url
    yield config, sync_url
    if prior_url is None:
        os.environ.pop("PULLBOX_DB_URL", None)
    else:
        os.environ["PULLBOX_DB_URL"] = prior_url


def test_placement_operation_schema_upgrades_downgrades_and_upgrades_again(
    placement_operation_migration_config: tuple[Config, str],
) -> None:
    config, sync_url = placement_operation_migration_config
    command.upgrade(config, "head")
    engine = create_engine(sync_url)
    try:
        inspector = inspect(engine)
        assert {
            "story_arc_placement_operations",
            "story_arc_placement_operation_actions",
        } <= set(inspector.get_table_names())
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
        } <= {column["name"] for column in inspector.get_columns("story_arc_placement_operations")}
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
        } <= {
            column["name"]
            for column in inspector.get_columns("story_arc_placement_operation_actions")
        }
        operation_indexes = {
            index["name"]: index["column_names"]
            for index in inspector.get_indexes("story_arc_placement_operations")
        }
        action_indexes = {
            index["name"]: index["column_names"]
            for index in inspector.get_indexes("story_arc_placement_operation_actions")
        }
        assert operation_indexes["ix_story_arc_placement_operations_arc_state"] == [
            "story_arc_id",
            "state",
            "id",
        ]
        assert action_indexes["ix_story_arc_placement_operation_actions_next"] == [
            "operation_id",
            "state",
            "sequence_number",
            "id",
        ]

        operation_foreign_keys = {
            tuple(foreign_key["constrained_columns"]): foreign_key["referred_table"]
            for foreign_key in inspector.get_foreign_keys("story_arc_placement_operations")
        }
        action_foreign_keys = {
            tuple(foreign_key["constrained_columns"]): foreign_key["referred_table"]
            for foreign_key in inspector.get_foreign_keys("story_arc_placement_operation_actions")
        }
        assert operation_foreign_keys == {
            ("story_arc_id",): "story_arcs",
            ("actor_user_id",): "users",
        }
        story_arc_foreign_key = next(
            foreign_key
            for foreign_key in inspector.get_foreign_keys("story_arc_placement_operations")
            if foreign_key["constrained_columns"] == ["story_arc_id"]
        )
        assert story_arc_foreign_key["options"]["ondelete"] == "RESTRICT"
        assert action_foreign_keys == {("operation_id",): "story_arc_placement_operations"}
        story_arc_columns = {column["name"] for column in inspector.get_columns("story_arcs")}
        assert {
            "active_placement_operation_token",
            "active_placement_operation_kind",
        } <= story_arc_columns
    finally:
        engine.dispose()

    command.downgrade(config, _PARENT_REVISION)
    engine = create_engine(sync_url)
    try:
        inspector = inspect(engine)
        assert "story_arc_placement_operations" not in inspector.get_table_names()
        assert "story_arc_placement_operation_actions" not in inspector.get_table_names()
        assert "active_placement_operation_token" not in {
            column["name"] for column in inspector.get_columns("story_arcs")
        }
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(sync_url)
    try:
        assert "story_arc_placement_operations" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_downgrade_refuses_to_erase_durable_operation_journal(
    placement_operation_migration_config: tuple[Config, str],
) -> None:
    config, sync_url = placement_operation_migration_config
    command.upgrade(config, "head")
    engine = create_engine(sync_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO story_arcs (name) VALUES ('Journal')"))
            story_arc_id = int(connection.execute(text("SELECT last_insert_rowid()")).scalar_one())
            connection.execute(
                text(
                    "INSERT INTO story_arc_placement_operations "
                    "(story_arc_id, operation_kind, state, active_fence_slot, "
                    "operation_token, idempotency_key_hash, expected_story_arc_revision, "
                    "confirmation_schema_version, confirmed_at, source_policy_snapshot, "
                    "proposed_policy_snapshot, source_root_fingerprint, "
                    "target_root_fingerprint, scope_digest, plan_digest, recovery_mode, "
                    "total_action_count, completed_action_count, failed_action_count, "
                    "required_bytes, attempt_count, last_result) VALUES "
                    "(:story_arc_id, 'policy_migration', 'ready', 1, :token, "
                    ":idempotency, 1, 1, CURRENT_TIMESTAMP, '{}', '{}', '{}', '{}', "
                    ":scope_digest, :plan_digest, 'rollback', 0, 0, 0, 0, 0, '{}')"
                ),
                {
                    "story_arc_id": story_arc_id,
                    "token": "5" * 32,
                    "idempotency": "5" * 64,
                    "scope_digest": "a" * 64,
                    "plan_digest": "b" * 64,
                },
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="placement-operation journal"):
        command.downgrade(config, _PARENT_REVISION)
