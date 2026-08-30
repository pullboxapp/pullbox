"""Add exact issue-number compatibility storage.

Revision ID: z1a2b3c4d567
Revises: y0z1a2b3c456
Create Date: 2026-08-30
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "z1a2b3c4d567"
down_revision: str | Sequence[str] | None = "y0z1a2b3c456"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BATCH_SIZE = 2_000
_MAX_TEXT_LENGTH = 320


def _format_issue_number(value: object) -> str:
    """Frozen migration formatter for legacy numeric issue values."""
    try:
        decimal_value = Decimal(str(value))
    except InvalidOperation:
        raise RuntimeError("Cannot backfill an invalid issue number.") from None
    if not decimal_value.is_finite():
        raise RuntimeError("Cannot backfill a non-finite issue number.")
    if decimal_value == 0:
        return "0"
    rendered = format(decimal_value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if not rendered or len(rendered) > _MAX_TEXT_LENGTH:
        raise RuntimeError("Cannot backfill an unsupported issue number.")
    return rendered


def _backfill_issue_number_text() -> None:
    connection = op.get_bind()
    last_id = 0
    select_batch = sa.text(
        "SELECT id, issue_number FROM issues WHERE id > :last_id ORDER BY id LIMIT :batch_size"
    )
    update_row = sa.text(
        "UPDATE issues SET issue_number_text = :issue_number_text WHERE id = :issue_id"
    )

    while True:
        rows = (
            connection.execute(
                select_batch,
                {"last_id": last_id, "batch_size": _BATCH_SIZE},
            )
            .mappings()
            .all()
        )
        if not rows:
            return
        updates = [
            {
                "issue_id": int(row["id"]),
                "issue_number_text": _format_issue_number(row["issue_number"]),
            }
            for row in rows
        ]
        connection.execute(update_row, updates)
        last_id = int(rows[-1]["id"])


def _assert_exact_text_is_unique() -> None:
    duplicate = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT series_id, issue_number_text FROM issues "
                "WHERE issue_number_text IS NOT NULL "
                "GROUP BY series_id, issue_number_text HAVING COUNT(*) > 1 LIMIT 1"
            )
        )
        .first()
    )
    if duplicate is not None:
        raise RuntimeError("Cannot add exact issue-number uniqueness while duplicates remain.")


def _assert_downgrade_is_lossless() -> None:
    connection = op.get_bind()
    last_id = 0
    select_batch = sa.text(
        "SELECT id, issue_number, issue_number_text FROM issues "
        "WHERE id > :last_id ORDER BY id LIMIT :batch_size"
    )
    while True:
        rows = (
            connection.execute(
                select_batch,
                {"last_id": last_id, "batch_size": _BATCH_SIZE},
            )
            .mappings()
            .all()
        )
        if not rows:
            return
        for row in rows:
            exact_text = row["issue_number_text"]
            if exact_text is not None and str(exact_text) != _format_issue_number(
                row["issue_number"]
            ):
                raise RuntimeError(
                    "Cannot downgrade while divergent exact issue-number text remains."
                )
        last_id = int(rows[-1]["id"])


def upgrade() -> None:
    """Add nullable exact text, backfill legacy rows, and index ordering."""
    op.add_column(
        "issues",
        sa.Column("issue_number_text", sa.String(320), nullable=True),
    )

    _backfill_issue_number_text()
    _assert_exact_text_is_unique()

    op.create_index(
        "uq_series_issue_number_text",
        "issues",
        ["series_id", "issue_number_text"],
        unique=True,
    )
    op.create_index(
        "ix_issues_series_number_order",
        "issues",
        ["series_id", "issue_number", "issue_number_text", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop exact text only when doing so cannot erase divergent semantics."""
    _assert_downgrade_is_lossless()

    op.drop_index("ix_issues_series_number_order", table_name="issues")
    op.drop_index("uq_series_issue_number_text", table_name="issues")
    op.drop_column("issues", "issue_number_text")
