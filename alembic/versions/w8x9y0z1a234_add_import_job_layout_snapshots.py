"""Add durable import-job layout and handling snapshots.

Revision ID: w8x9y0z1a234
Revises: v7w8x9y0z123
Create Date: 2026-08-29
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "w8x9y0z1a234"
down_revision: str | Sequence[str] | None = "v7w8x9y0z123"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AUTO_LAYOUT_JSON = (
    '{"schema_version":1,"mode":"auto","preset":null,'
    '"series_path_template":null,"issue_filename_template":null,'
    '"selected_cluster_id":null,"fallback_to_auto":true}'
)


def upgrade() -> None:
    """Add compatible defaults for existing and newly-created jobs."""
    with op.batch_alter_table("import_jobs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "file_handling_mode",
                sa.Enum(
                    "managed_copy",
                    "in_place",
                    name="importfilehandlingmode",
                    native_enum=False,
                    create_constraint=True,
                ),
                nullable=False,
                server_default="managed_copy",
            )
        )
        batch_op.add_column(
            sa.Column(
                "source_layout_snapshot",
                sa.JSON(),
                nullable=False,
                server_default=_AUTO_LAYOUT_JSON,
            )
        )
        batch_op.add_column(
            sa.Column(
                "future_layout_requested",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(sa.Column("future_root_policy_snapshot", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "future_root_policy_applied_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )


def downgrade() -> None:
    """Remove durable import-job snapshot fields."""
    with op.batch_alter_table("import_jobs") as batch_op:
        batch_op.drop_constraint("importfilehandlingmode", type_="check")
        batch_op.drop_column("future_root_policy_applied_at")
        batch_op.drop_column("future_root_policy_snapshot")
        batch_op.drop_column("future_layout_requested")
        batch_op.drop_column("source_layout_snapshot")
        batch_op.drop_column("file_handling_mode")
