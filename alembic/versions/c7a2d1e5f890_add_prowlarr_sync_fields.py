"""add prowlarr sync fields to indexer_configs

Revision ID: c7a2d1e5f890
Revises: b5e2a8f41c03, e4db993bc3df
Create Date: 2026-02-28 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7a2d1e5f890"
down_revision: str | Sequence[str] | None = "b5e2a8f41c03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add Prowlarr sync and search capability fields to indexer_configs."""
    op.add_column(
        "indexer_configs",
        sa.Column("source", sa.String(20), nullable=False, server_default="manual"),
    )
    op.add_column(
        "indexer_configs",
        sa.Column("prowlarr_indexer_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "indexer_configs",
        sa.Column("enable_rss", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "indexer_configs",
        sa.Column(
            "enable_automatic_search", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )
    op.add_column(
        "indexer_configs",
        sa.Column(
            "enable_interactive_search", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )


def downgrade() -> None:
    """Remove Prowlarr sync and search capability fields."""
    op.drop_column("indexer_configs", "enable_interactive_search")
    op.drop_column("indexer_configs", "enable_automatic_search")
    op.drop_column("indexer_configs", "enable_rss")
    op.drop_column("indexer_configs", "prowlarr_indexer_id")
    op.drop_column("indexer_configs", "source")
