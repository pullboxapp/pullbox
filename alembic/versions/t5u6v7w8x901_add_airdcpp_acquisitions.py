"""Add durable AirDC++ acquisition provenance.

Revision ID: t5u6v7w8x901
Revises: s4t5u6v7w890
Create Date: 2026-08-25
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "t5u6v7w8x901"
down_revision: str | Sequence[str] | None = "s4t5u6v7w890"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "airdcpp_acquisitions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("download_history_id", sa.Integer(), nullable=False),
        sa.Column("request_key", sa.String(length=255), nullable=False),
        sa.Column("client_config_id", sa.Integer(), nullable=True),
        sa.Column("client_identity", sa.String(length=255), nullable=False),
        sa.Column("search_log_id", sa.Integer(), nullable=True),
        sa.Column("tth", sa.String(length=39), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("original_name", sa.String(length=500), nullable=False),
        sa.Column("search_instance_id", sa.BigInteger(), nullable=True),
        sa.Column("grouped_result_id", sa.String(length=1000), nullable=True),
        sa.Column("result_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bundle_id", sa.BigInteger(), nullable=True),
        sa.Column("client_state", sa.String(length=100), nullable=True),
        sa.Column("remote_target", sa.String(length=1000), nullable=True),
        sa.Column("route_snapshot", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_retries", sa.Integer(), server_default="3", nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciliation_error", sa.Text(), nullable=True),
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
        sa.CheckConstraint("size_bytes > 0", name="ck_airdcpp_acquisition_size_positive"),
        sa.CheckConstraint(
            "retry_count >= 0",
            name="ck_airdcpp_acquisition_retry_nonnegative",
        ),
        sa.CheckConstraint(
            "max_retries >= 0",
            name="ck_airdcpp_acquisition_max_retry_nonnegative",
        ),
        sa.CheckConstraint(
            "retry_count <= max_retries",
            name="ck_airdcpp_acquisition_retry_within_max",
        ),
        sa.ForeignKeyConstraint(
            ["download_history_id"],
            ["download_history.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["client_config_id"],
            ["download_client_configs.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["search_log_id"],
            ["search_logs.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "download_history_id",
            name="uq_airdcpp_acquisition_history",
        ),
        sa.UniqueConstraint("request_key", name="uq_airdcpp_acquisition_request_key"),
        sa.UniqueConstraint(
            "client_identity",
            "bundle_id",
            name="uq_airdcpp_acquisition_client_bundle",
        ),
    )
    op.create_index(
        "ix_airdcpp_acquisition_client_bundle",
        "airdcpp_acquisitions",
        ["client_config_id", "bundle_id"],
    )
    op.create_index(
        "ix_airdcpp_acquisition_client_reconciled",
        "airdcpp_acquisitions",
        ["client_config_id", "last_reconciled_at"],
    )
    op.create_index(
        "ix_airdcpp_acquisition_content",
        "airdcpp_acquisitions",
        ["tth", "size_bytes"],
    )
    op.create_index(
        "ix_airdcpp_acquisition_next_retry",
        "airdcpp_acquisitions",
        ["next_retry_at"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    count = connection.execute(sa.text("SELECT COUNT(*) FROM airdcpp_acquisitions")).scalar_one()
    if count:
        raise RuntimeError("Refusing to discard AirDC++ acquisition provenance")
    op.drop_table("airdcpp_acquisitions")
