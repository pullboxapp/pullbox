"""Use ORM-readable issue classifications for historical rows and new defaults."""

import sqlalchemy as sa

from alembic import op

revision = "p3d456789abc"
down_revision = "o2c3456789ab"
branch_labels = None
depends_on = None

_LABELS = (
    "issue",
    "annual",
    "tpb",
    "omnibus",
    "gn",
    "ogn",
    "hc",
    "one_shot",
    "special",
    "deluxe",
    "compendium",
    "volume",
)
_ISSUES = sa.table("issues", sa.column("issue_type", sa.String(20)))


def _check_rebuild() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "sqlite" and conn.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM sqlite_schema WHERE type='trigger' AND tbl_name='issues') "
            "OR EXISTS(SELECT 1 FROM pragma_index_list('issues') i, pragma_index_xinfo(i.name) x "
            "WHERE x.cid=-2)"
        )
    ):
        raise RuntimeError("Cannot safely rebuild custom SQLite issue schema; review a backup.")


def _default(value: str) -> None:
    with op.batch_alter_table("issues") as batch:
        batch.alter_column(
            "issue_type", existing_type=sa.String(20), existing_nullable=False, server_default=value
        )


def upgrade() -> None:
    _check_rebuild()
    conn = op.get_bind()
    kind = _ISSUES.c.issue_type
    known = (*_LABELS, *(label.upper() for label in _LABELS))
    if conn.scalar(sa.select(1).select_from(_ISSUES).where(kind.not_in(known)).limit(1)):
        raise RuntimeError("Unrecognized issue classification. Review a backup before upgrading.")
    conn.execute(_ISSUES.update().where(kind.in_(_LABELS)).values(issue_type=sa.func.upper(kind)))
    _default("ISSUE")


def downgrade() -> None:
    # The older ORM already reads canonical uppercase labels. Do not undo user classifications.
    _check_rebuild()
    _default("issue")
