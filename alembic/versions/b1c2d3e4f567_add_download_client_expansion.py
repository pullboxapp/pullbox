"""add download client expansion columns

Revision ID: b1c2d3e4f567
Revises: a9eff3b660f5
Create Date: 2026-03-17 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f567"
down_revision: str | Sequence[str] | None = "a9eff3b660f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add NZBGet, Transmission, and Deluge columns to download_client_configs."""
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE downloadclienttype ADD VALUE IF NOT EXISTS 'NZBGET'")
        op.execute("ALTER TYPE downloadclienttype ADD VALUE IF NOT EXISTS 'TRANSMISSION'")
        op.execute("ALTER TYPE downloadclienttype ADD VALUE IF NOT EXISTS 'DELUGE'")
    # NZBGet-specific options
    op.add_column(
        "download_client_configs",
        sa.Column("nzbget_priority", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "download_client_configs",
        sa.Column("nzbget_post_processing", sa.String(length=20), nullable=True),
    )

    # Transmission-specific options
    op.add_column(
        "download_client_configs",
        sa.Column("transmission_download_dir", sa.String(length=1000), nullable=True),
    )
    op.add_column(
        "download_client_configs",
        sa.Column("transmission_bandwidth_priority", sa.Integer(), nullable=True),
    )
    op.add_column(
        "download_client_configs",
        sa.Column("transmission_seed_ratio_limit", sa.Float(), nullable=True),
    )
    op.add_column(
        "download_client_configs",
        sa.Column("transmission_seed_idle_limit", sa.Integer(), nullable=True),
    )

    # Deluge-specific options
    op.add_column(
        "download_client_configs",
        sa.Column("deluge_label", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "download_client_configs",
        sa.Column("deluge_max_ratio", sa.Float(), nullable=True),
    )
    op.add_column(
        "download_client_configs",
        sa.Column("deluge_move_completed_path", sa.String(length=1000), nullable=True),
    )


def downgrade() -> None:
    """Remove NZBGet, Transmission, and Deluge columns."""
    op.drop_column("download_client_configs", "deluge_move_completed_path")
    op.drop_column("download_client_configs", "deluge_max_ratio")
    op.drop_column("download_client_configs", "deluge_label")
    op.drop_column("download_client_configs", "transmission_seed_idle_limit")
    op.drop_column("download_client_configs", "transmission_seed_ratio_limit")
    op.drop_column("download_client_configs", "transmission_bandwidth_priority")
    op.drop_column("download_client_configs", "transmission_download_dir")
    op.drop_column("download_client_configs", "nzbget_post_processing")
    op.drop_column("download_client_configs", "nzbget_priority")
