"""Migration coverage for the durable story-arc synchronization outbox."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command

if TYPE_CHECKING:
    from collections.abc import Generator

_ALEMBIC_DIR = Path(__file__).resolve().parent.parent.parent / "alembic"
_PARENT_REVISION = "a2b3c4d5e678"


@pytest.fixture
def migration_config(tmp_path: Path) -> Generator[tuple[Config, str], None, None]:
    db_path = tmp_path / "story-arc-sync-migration.db"
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


def test_story_arc_sync_work_upgrade_and_downgrade(
    migration_config: tuple[Config, str],
) -> None:
    config, sync_url = migration_config
    command.upgrade(config, "head")
    engine = create_engine(sync_url)
    try:
        inspector = inspect(engine)
        assert "story_arc_sync_work" in inspector.get_table_names()
        assert {
            "issue_story_arc_id",
            "library_file_id",
            "desired_generation",
            "source_signature_hash",
            "source_file_path",
            "source_file_size",
            "source_file_modified_at",
            "source_file_hash",
            "source_signature_schema_version",
            "source_signature_resolved_path",
            "source_signature_size",
            "source_signature_mtime_ns",
            "source_signature_device",
            "source_signature_inode",
            "story_arc_revision",
            "membership_sequence",
            "policy_schema_version",
            "reason",
            "state",
            "attempt_count",
            "next_attempt_at",
            "claim_token",
            "claimed_at",
            "last_error_code",
            "last_error_category",
            "last_error_detail",
            "last_result",
        } <= {column["name"] for column in inspector.get_columns("story_arc_sync_work")}
        indexes = {
            index["name"]: index["column_names"]
            for index in inspector.get_indexes("story_arc_sync_work")
        }
        assert indexes["ix_story_arc_sync_work_ready"] == [
            "state",
            "next_attempt_at",
            "id",
        ]
    finally:
        engine.dispose()

    command.downgrade(config, _PARENT_REVISION)
    engine = create_engine(sync_url)
    try:
        inspector = inspect(engine)
        assert "story_arc_sync_work" not in inspector.get_table_names()
        assert "operation_token" not in {
            column["name"] for column in inspector.get_columns("story_arc_placements")
        }
    finally:
        engine.dispose()
