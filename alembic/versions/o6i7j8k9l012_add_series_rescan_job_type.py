"""Allow durable per-series rescan jobs in the existing utility queue."""

from alembic import op
from sqlalchemy import text

revision = "o6i7j8k9l012"
down_revision = "n5h6i7j8k901"
branch_labels = None
depends_on = None

_OLD_TYPES = (
    "file_convert",
    "mass_convert_pipeline",
    "mass_rename",
    "db_check_cleanup",
    "export_library",
    "integrity_check",
    "library_permissions",
    "rollback",
)


def _replace(values: tuple[str, ...]) -> None:
    with op.batch_alter_table("utility_jobs") as batch:
        batch.drop_constraint("ck_utility_jobs_job_type", type_="check")
        batch.create_check_constraint(
            "ck_utility_jobs_job_type", "job_type IN (" + ", ".join(repr(v) for v in values) + ")"
        )


def upgrade() -> None:
    _replace((*_OLD_TYPES, "series_rescan"))


def downgrade() -> None:
    if op.get_bind().scalar(
        text("SELECT count(*) FROM utility_jobs WHERE job_type = 'series_rescan'")
    ):
        raise RuntimeError("Remove saved series rescan jobs before downgrading this migration.")
    _replace(_OLD_TYPES)
