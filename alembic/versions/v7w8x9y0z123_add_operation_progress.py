"""Add shared durable operation progress projection.

Revision ID: v7w8x9y0z123
Revises: u6v7w8x9y012
Create Date: 2026-08-27
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "v7w8x9y0z123"
down_revision: str | Sequence[str] | None = "u6v7w8x9y012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operation_progress",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "operation_type",
            sa.Enum(
                "import",
                "download",
                "post_processing",
                "issue_import",
                "orphan_recovery",
                "utility",
                name="operationprogresstype",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("operation_key", sa.String(length=160), nullable=False),
        sa.Column("group_key", sa.String(length=160), nullable=True),
        sa.Column(
            "state",
            sa.Enum(
                "queued",
                "running",
                "paused",
                "retrying",
                "completed",
                "failed",
                "cancelled",
                name="operationprogressstate",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("phase", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("message", sa.Text(), server_default="", nullable=False),
        sa.Column("source_label", sa.String(length=255), nullable=True),
        sa.Column("detail_url", sa.String(length=1000), nullable=True),
        sa.Column(
            "visibility",
            sa.Enum(
                "prominent",
                "passive",
                "quiet",
                name="operationprogressvisibility",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="prominent",
            nullable=False,
        ),
        sa.Column(
            "tone",
            sa.Enum(
                "info",
                "success",
                "warning",
                "danger",
                name="operationprogresstone",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="info",
            nullable=False,
        ),
        sa.Column("attention_required", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("overall_current", sa.BigInteger(), nullable=True),
        sa.Column("overall_total", sa.BigInteger(), nullable=True),
        sa.Column("overall_percent", sa.Float(), nullable=True),
        sa.Column("overall_unit", sa.String(length=40), nullable=True),
        sa.Column("overall_indeterminate", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("item_key", sa.String(length=255), nullable=True),
        sa.Column("item_label", sa.String(length=500), nullable=True),
        sa.Column("item_phase", sa.String(length=100), nullable=True),
        sa.Column("item_message", sa.Text(), nullable=True),
        sa.Column("item_current", sa.BigInteger(), nullable=True),
        sa.Column("item_total", sa.BigInteger(), nullable=True),
        sa.Column("item_percent", sa.Float(), nullable=True),
        sa.Column("item_unit", sa.String(length=40), nullable=True),
        sa.Column("item_indeterminate", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("rate", sa.Float(), nullable=True),
        sa.Column("rate_unit", sa.String(length=40), nullable=True),
        sa.Column("eta_seconds", sa.Integer(), nullable=True),
        sa.Column("detail_snapshot", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.CheckConstraint("revision >= 0", name="ck_operation_progress_revision_nonnegative"),
        sa.CheckConstraint(
            "overall_percent IS NULL OR (overall_percent >= 0 AND overall_percent <= 100)",
            name="ck_operation_progress_overall_percent",
        ),
        sa.CheckConstraint(
            "item_percent IS NULL OR (item_percent >= 0 AND item_percent <= 100)",
            name="ck_operation_progress_item_percent",
        ),
        sa.CheckConstraint(
            "overall_current IS NULL OR overall_current >= 0",
            name="ck_operation_progress_overall_current",
        ),
        sa.CheckConstraint(
            "overall_total IS NULL OR overall_total >= 0",
            name="ck_operation_progress_overall_total",
        ),
        sa.CheckConstraint(
            "item_current IS NULL OR item_current >= 0",
            name="ck_operation_progress_item_current",
        ),
        sa.CheckConstraint(
            "item_total IS NULL OR item_total >= 0",
            name="ck_operation_progress_item_total",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_type",
            "operation_key",
            name="uq_operation_progress_identity",
        ),
    )
    op.create_index(
        "ix_operation_progress_activity",
        "operation_progress",
        ["visibility", "state", "attention_required", "updated_at"],
    )
    op.create_index(
        "ix_operation_progress_group",
        "operation_progress",
        ["group_key", "updated_at"],
    )


def downgrade() -> None:
    op.drop_table("operation_progress")
