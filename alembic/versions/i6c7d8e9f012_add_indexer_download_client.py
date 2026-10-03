"""Let an indexer send every grab to one chosen download client.

Revision ID: i6c7d8e9f012
Revises: h5b6c7d8e901
Create Date: 2026-10-03
"""

import sqlalchemy as sa

from alembic import op

revision = "i6c7d8e9f012"
down_revision = "h5b6c7d8e901"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable client pin; existing indexers stay unpinned."""
    with op.batch_alter_table("indexer_configs") as batch_op:
        batch_op.add_column(sa.Column("download_client_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_indexer_configs_download_client",
            "download_client_configs",
            ["download_client_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Drop the pin; those indexers fall back to priority-based selection."""
    with op.batch_alter_table("indexer_configs") as batch_op:
        batch_op.drop_constraint("fk_indexer_configs_download_client", type_="foreignkey")
        batch_op.drop_column("download_client_id")
