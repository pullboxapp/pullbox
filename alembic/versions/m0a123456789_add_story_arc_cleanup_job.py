"""Allow reviewed Story Arc cleanup jobs in the existing utility queue."""

from sqlalchemy import text

from alembic import op

revision = "m0a123456789"
down_revision = "l9f012345678"
branch_labels = None
depends_on = None

_OLD = (
    "file_convert",
    "mass_convert_pipeline",
    "mass_rename",
    "db_check_cleanup",
    "series_rescan",
    "export_library",
    "integrity_check",
    "library_permissions",
    "rollback",
    "file_metadata",
)


def _replace(values: tuple[str, ...]) -> None:
    with op.batch_alter_table("utility_jobs") as batch:
        batch.drop_constraint("ck_utility_jobs_job_type", type_="check")
        batch.create_check_constraint(
            "ck_utility_jobs_job_type", "job_type IN (" + ", ".join(repr(v) for v in values) + ")"
        )


def upgrade() -> None:
    _replace((*_OLD, "story_arc_cleanup"))


def downgrade() -> None:
    if op.get_bind().scalar(
        text("SELECT count(*) FROM utility_jobs WHERE job_type = 'story_arc_cleanup'")
    ):
        raise RuntimeError("Remove saved Story Arc cleanup jobs before downgrading.")
    _replace(_OLD)
