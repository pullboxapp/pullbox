"""Persist explicit Watch intent independently of release caches."""

import sqlalchemy as sa

from alembic import op

revision = "i6c7d8e9f012"
down_revision = "h5b6c7d8e901"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "series_interests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_namespace", sa.String(32), nullable=False),
        sa.Column("source_series_id", sa.String(32), nullable=False),
        sa.Column("title_snapshot", sa.String(512), nullable=False),
        sa.Column("publisher_snapshot", sa.String(255), nullable=False),
        sa.Column("year_snapshot", sa.Integer()),
        sa.Column("next_known_release_date", sa.Date()),
        sa.Column(
            "target_library_root_id",
            sa.Integer(),
            sa.ForeignKey("library_roots.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "resolved_series_id", sa.Integer(), sa.ForeignKey("series.id", ondelete="SET NULL")
        ),
        sa.Column(
            "last_actor_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")
        ),
        sa.Column(
            "state",
            sa.Enum(
                "watching",
                "needs_confirmation",
                "promoted",
                "cancelled",
                name="series_interest_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "source_namespace", "source_series_id", name="uq_series_interest_source"
        ),
        sa.CheckConstraint("source_namespace = 'locg'", name="ck_series_interest_namespace"),
    )
    op.create_index("ix_series_interests_state", "series_interests", ["state"])


def downgrade() -> None:
    table = sa.table("series_interests", sa.column("id"))
    if op.get_bind().scalar(sa.select(sa.func.count()).select_from(table)):
        raise RuntimeError("Cannot downgrade retained Watch decisions.")
    op.drop_table("series_interests")
