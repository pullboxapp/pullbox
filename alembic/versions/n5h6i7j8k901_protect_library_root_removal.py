"""Protect root dependencies and retain removed import destinations.

Revision ID: n5h6i7j8k901
Revises: m4g5h6i7j890
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "n5h6i7j8k901"
down_revision = "m4g5h6i7j890"
branch_labels = None
depends_on = None

_TABLES = ("library_files", "series", "story_arcs", "story_arc_placements", "import_jobs")
_NAMING = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}
_SQLITE_INLINE_KEYS = {
    "import_jobs": {"story_arc_rollback_waiting_work_id": "story_arc_sync_work"},
    "story_arcs": {
        "source_import_job_id": "import_jobs",
        "target_library_root_id": "library_roots",
    },
}
_STORY_ARC_INLINE_CHECKS = {
    "source_kind": "VARCHAR(9) NOT NULL DEFAULT 'legacy' CONSTRAINT storyarcsourcekind "
    "CHECK (source_kind IN ('legacy','pullbox','mylar3','folder','comicinfo','provider'))",
    "lifecycle": "VARCHAR(8) NOT NULL DEFAULT 'active' CONSTRAINT storyarclifecycle "
    "CHECK (lifecycle IN ('active','archived'))",
}


def _restore_inline_keys(bind: sa.Connection, table: sa.Table) -> None:
    """Restore ADD COLUMN FKs expected by immutable older SQLite downgrades."""
    definitions = {
        column: f'INTEGER REFERENCES "{target}"(id) ON DELETE SET NULL'
        for column, target in _SQLITE_INLINE_KEYS.get(table.name, {}).items()
    }
    if table.name == "story_arcs":
        definitions.update(_STORY_ARC_INLINE_CHECKS)
    for column, definition in definitions.items():
        if column not in table.c:
            continue
        indexes = [index for index in table.indexes if column in index.columns]
        bind.exec_driver_sql(
            f'CREATE TEMP TABLE root_removal_refs AS SELECT id, "{column}" FROM "{table.name}"'
        )
        bind.exec_driver_sql("CREATE UNIQUE INDEX root_removal_refs_id ON root_removal_refs(id)")
        for index in indexes:
            index.drop(bind)
        op.drop_column(table.name, column)
        bind.exec_driver_sql(f'ALTER TABLE "{table.name}" ADD COLUMN "{column}" {definition}')
        bind.exec_driver_sql(
            f'UPDATE "{table.name}" SET "{column}" = (SELECT "{column}" FROM root_removal_refs '
            f'WHERE root_removal_refs.id = "{table.name}".id)'
        )
        bind.exec_driver_sql("DROP TABLE root_removal_refs")
        for index in indexes:
            index.create(bind)


def _change_constraints(*, upgrading: bool) -> None:
    bind = op.get_bind()
    # Batch recreation with enforcement enabled can cascade into child tables.
    if bind.dialect.name == "sqlite" and bind.exec_driver_sql("PRAGMA foreign_keys").scalar():
        raise RuntimeError(
            "Run root-removal migration with the standalone Alembic connection (foreign_keys=OFF)."
        )
    for table in _TABLES:
        metadata = sa.MetaData(naming_convention=_NAMING)
        reflected = sa.Table(table, metadata, autoload_with=bind)
        if bind.dialect.name == "sqlite":
            # SQLAlchemy's SQL-text reflection can lose ON DELETE on columns
            # originally added with ALTER TABLE. SQLite's own FK list is exact.
            actions = {
                row[3]: (row[5], row[6])
                for row in bind.exec_driver_sql(f'PRAGMA foreign_key_list("{table}")')
            }
            for fk in list(reflected.foreign_key_constraints):
                if len(fk.columns) == 1:
                    column = next(iter(fk.columns)).name
                    fk.onupdate, fk.ondelete = actions[column]
                    if not upgrading and column in _SQLITE_INLINE_KEYS.get(table, {}):
                        reflected.constraints.remove(fk)
            if not upgrading and table == "story_arcs":
                for constraint in list(reflected.constraints):
                    if isinstance(constraint, sa.CheckConstraint) and constraint.name in {
                        "storyarcsourcekind",
                        "storyarclifecycle",
                    }:
                        reflected.constraints.remove(constraint)
        keys = [
            key
            for key in sa.inspect(bind).get_foreign_keys(table)
            if key["referred_table"] == "library_roots"
        ]
        with op.batch_alter_table(
            table,
            naming_convention=_NAMING,
            copy_from=reflected,
            recreate="always" if bind.dialect.name == "sqlite" else "auto",
        ) as batch:
            for key in keys:
                columns = key["constrained_columns"]
                if (
                    not upgrading
                    and bind.dialect.name == "sqlite"
                    and columns[0] in _SQLITE_INLINE_KEYS.get(table, {})
                ):
                    continue
                name = key["name"] or f"fk_{table}_{columns[0]}_library_roots"
                batch.drop_constraint(name, type_="foreignkey")
                batch.create_foreign_key(
                    name,
                    "library_roots",
                    columns,
                    ["id"],
                    ondelete="RESTRICT"
                    if upgrading
                    else ("CASCADE" if table == "library_files" else "SET NULL"),
                )
        if not upgrading and bind.dialect.name == "sqlite":
            _restore_inline_keys(bind, reflected)
    if bind.dialect.name == "sqlite" and bind.exec_driver_sql("PRAGMA foreign_key_check").first():
        raise RuntimeError("Foreign key validation failed after root-removal migration.")


def upgrade() -> None:
    _change_constraints(upgrading=True)
    op.add_column(
        "import_jobs", sa.Column("removed_library_root_snapshot", sa.JSON(), nullable=True)
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM import_jobs WHERE removed_library_root_snapshot IS NOT NULL LIMIT 1"
            )
        )
        .first()
    ):
        raise RuntimeError("Cannot downgrade while import history records removed library roots.")
    _change_constraints(upgrading=False)
    op.drop_column("import_jobs", "removed_library_root_snapshot")
