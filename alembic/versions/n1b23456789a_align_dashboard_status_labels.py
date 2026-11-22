"""Align database status labels used by the signed-in dashboard."""

import sqlalchemy as sa

from alembic import op

revision = "n1b23456789a"
down_revision = "m0a123456789"
branch_labels = None
depends_on = None

_LABELS = ("pending", "accepted", "dismissed")
_SUGGESTIONS = sa.table("matching_suggestions", sa.column("status", sa.String()))


def _normalize(*, lowercase: bool) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        # The native PostgreSQL enum has always enforced the lowercase labels.
        return
    uppercase = tuple(value.upper() for value in _LABELS)
    status = _SUGGESTIONS.c.status
    unexpected = sa.select(1).where(status.not_in((*_LABELS, *uppercase))).limit(1)
    if bind.execute(unexpected).first():
        raise RuntimeError("Unrecognized matching suggestion status; no suggestions were changed.")
    source, convert = (uppercase, sa.func.lower) if lowercase else (_LABELS, sa.func.upper)
    bind.execute(_SUGGESTIONS.update().where(status.in_(source)).values(status=convert(status)))


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE downloadstate ADD VALUE IF NOT EXISTS 'RETRY_PENDING'")
        op.execute("ALTER TYPE fileformat ADD VALUE IF NOT EXISTS 'CBT'")
        op.execute("ALTER TYPE fileformat ADD VALUE IF NOT EXISTS 'EPUB'")
        return
    _normalize(lowercase=True)


def downgrade() -> None:
    # Return all SQLite rows to the spelling the previous ORM could read.
    # Removing PostgreSQL labels would break existing retry history and library files.
    _normalize(lowercase=False)
