"""Make exact issue-number text the canonical per-series identity.

Revision ID: e6f7a8b9c012
Revises: d5e6f7a8b901
Create Date: 2026-08-30
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "e6f7a8b9c012"
down_revision: str | Sequence[str] | None = "d5e6f7a8b901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_NUMERIC_UNIQUE = "uq_series_issue"


def _drop_legacy_numeric_unique() -> None:
    if op.get_bind().dialect.name == "sqlite":
        # SQLite stores a table-level UNIQUE constraint in an auto-index, so
        # removing it requires a bounded table rebuild. Alembic copies rows and
        # recreates the exact-text and ordering indexes during the batch.
        with op.batch_alter_table("issues", recreate="always") as batch_op:
            batch_op.drop_constraint(_LEGACY_NUMERIC_UNIQUE, type_="unique")
        return
    op.drop_constraint(_LEGACY_NUMERIC_UNIQUE, "issues", type_="unique")


def _assert_numeric_uniqueness_can_be_restored() -> None:
    duplicate = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT series_id, issue_number FROM issues "
                "GROUP BY series_id, issue_number HAVING COUNT(*) > 1 LIMIT 1"
            )
        )
        .first()
    )
    if duplicate is not None:
        raise RuntimeError(
            "Cannot downgrade while exact issue-number siblings share a numeric value."
        )


def _restore_legacy_numeric_unique() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("issues", recreate="always") as batch_op:
            batch_op.create_unique_constraint(
                _LEGACY_NUMERIC_UNIQUE,
                ["series_id", "issue_number"],
            )
        return
    op.create_unique_constraint(
        _LEGACY_NUMERIC_UNIQUE,
        "issues",
        ["series_id", "issue_number"],
    )


def upgrade() -> None:
    """Remove the float identity constraint while retaining its sort index."""
    _drop_legacy_numeric_unique()


def downgrade() -> None:
    """Restore float uniqueness only when no exact siblings would be lost."""
    _assert_numeric_uniqueness_can_be_restored()
    _restore_legacy_numeric_unique()
