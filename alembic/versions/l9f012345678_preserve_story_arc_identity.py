"""Never assign a deleted Story Arc's reader and worker target to a new arc."""

from alembic import op

revision = "l9f012345678"
down_revision = "k8e9f0123456"
branch_labels = None
depends_on = None


def _sqlite_identity(enabled: bool) -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(
            "story_arcs", recreate="always", table_kwargs={"sqlite_autoincrement": enabled}
        ):
            pass


def upgrade() -> None:
    _sqlite_identity(True)


def downgrade() -> None:
    _sqlite_identity(False)
