"""Backfill year_end for ended series missing a closed range.

Revision ID: v7q8r9s0t123
Revises: u6p7q8r9s012
Create Date: 2026-05-02
"""

from __future__ import annotations

from datetime import date

import sqlalchemy as sa

from alembic import op

revision = "v7q8r9s0t123"
down_revision = "u6p7q8r9s012"
branch_labels = None
depends_on = None


def _coerce_year(value: object) -> int | None:
    """Return a 4-digit year from a SQL result or date-like object."""
    if value is None:
        return None
    if isinstance(value, date):
        return value.year
    text = str(value).strip()
    if len(text) >= 4 and text[:4].isdigit():
        return int(text[:4])
    return None


def upgrade() -> None:
    """Backfill ended series rows with a closed year range."""
    bind = op.get_bind()

    series = sa.table(
        "series",
        sa.column("id", sa.Integer()),
        sa.column("year_start", sa.Integer()),
        sa.column("year_end", sa.Integer()),
        sa.column("status", sa.Enum("CONTINUING", "ENDED", "UNKNOWN", name="seriesstatus")),
    )
    issues = sa.table(
        "issues",
        sa.column("series_id", sa.Integer()),
        sa.column("release_date", sa.Date()),
        sa.column("store_date", sa.Date()),
    )

    ended_rows = bind.execute(
        sa.select(series.c.id, series.c.year_start).where(
            series.c.status == "ENDED",
            series.c.year_start.is_not(None),
            series.c.year_end.is_(None),
        )
    ).fetchall()

    for row in ended_rows:
        latest_release = bind.execute(
            sa.select(
                sa.func.max(sa.func.coalesce(issues.c.release_date, issues.c.store_date))
            ).where(issues.c.series_id == row.id)
        ).scalar()
        resolved_year_end = _coerce_year(latest_release) or row.year_start
        bind.execute(
            sa.update(series).where(series.c.id == row.id).values(year_end=resolved_year_end)
        )


def downgrade() -> None:
    """Irreversible data repair; keep derived year_end values intact."""
    return None
