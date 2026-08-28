"""Add bounded AirDC++ client settings.

Revision ID: s4t5u6v7w890
Revises: r3s4t5u6v789
Create Date: 2026-08-25
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "s4t5u6v7w890"
down_revision: str | Sequence[str] | None = "r3s4t5u6v789"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create settings only; configurations opt in through the application."""
    op.create_table(
        "airdcpp_client_settings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("client_config_id", sa.Integer(), nullable=False),
        sa.Column("search_enabled", sa.Boolean(), server_default="1", nullable=False),
        sa.Column(
            "automatic_search_enabled",
            sa.Boolean(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "minimum_search_interval_seconds",
            sa.Integer(),
            server_default="45",
            nullable=False,
        ),
        sa.Column(
            "manual_collection_seconds",
            sa.Integer(),
            server_default="8",
            nullable=False,
        ),
        sa.Column(
            "automatic_collection_seconds",
            sa.Integer(),
            server_default="15",
            nullable=False,
        ),
        sa.Column("max_results", sa.Integer(), server_default="200", nullable=False),
        sa.Column(
            "max_retained_routes",
            sa.Integer(),
            server_default="400",
            nullable=False,
        ),
        sa.Column(
            "max_concurrent_searches",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
        sa.Column(
            "request_timeout_seconds",
            sa.Integer(),
            server_default="15",
            nullable=False,
        ),
        sa.Column(
            "search_dispatch_deadline_seconds",
            sa.Integer(),
            server_default="45",
            nullable=False,
        ),
        sa.Column(
            "reconciliation_interval_seconds",
            sa.Integer(),
            server_default="30",
            nullable=False,
        ),
        sa.Column("hub_allowlist", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("queue_priority", sa.Integer(), nullable=True),
        sa.Column("next_search_allowed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "minimum_search_interval_seconds BETWEEN 45 AND 3600",
            name="ck_airdcpp_settings_minimum_search_interval",
        ),
        sa.CheckConstraint(
            "manual_collection_seconds BETWEEN 1 AND 120",
            name="ck_airdcpp_settings_manual_collection",
        ),
        sa.CheckConstraint(
            "automatic_collection_seconds BETWEEN 1 AND 120",
            name="ck_airdcpp_settings_automatic_collection",
        ),
        sa.CheckConstraint(
            "max_results BETWEEN 1 AND 1000",
            name="ck_airdcpp_settings_max_results",
        ),
        sa.CheckConstraint(
            "max_retained_routes BETWEEN max_results AND 2000",
            name="ck_airdcpp_settings_retained_routes",
        ),
        sa.CheckConstraint(
            "max_concurrent_searches BETWEEN 1 AND 4",
            name="ck_airdcpp_settings_concurrent_searches",
        ),
        sa.CheckConstraint(
            "request_timeout_seconds BETWEEN 1 AND 120",
            name="ck_airdcpp_settings_request_timeout",
        ),
        sa.CheckConstraint(
            "search_dispatch_deadline_seconds BETWEEN 5 AND 300",
            name="ck_airdcpp_settings_dispatch_deadline",
        ),
        sa.CheckConstraint(
            "reconciliation_interval_seconds BETWEEN 10 AND 300",
            name="ck_airdcpp_settings_reconciliation_interval",
        ),
        sa.CheckConstraint(
            "queue_priority IS NULL OR queue_priority BETWEEN -1 AND 6",
            name="ck_airdcpp_settings_queue_priority",
        ),
        sa.ForeignKeyConstraint(
            ["client_config_id"],
            ["download_client_configs.id"],
            name="fk_airdcpp_settings_client_config",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "client_config_id",
            name="uq_airdcpp_settings_client_config",
        ),
    )


def downgrade() -> None:
    """Remove the AirDC++ settings extension table."""
    op.drop_table("airdcpp_client_settings")
