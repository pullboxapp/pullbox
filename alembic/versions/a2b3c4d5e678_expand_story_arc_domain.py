"""Expand story arcs into an import-safe first-class domain.

Revision ID: a2b3c4d5e678
Revises: z1a2b3c4d567
Create Date: 2026-08-30
"""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "a2b3c4d5e678"
down_revision: str | Sequence[str] | None = "z1a2b3c4d567"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BATCH_SIZE = 2_000
_SOURCE_KINDS = ("legacy", "pullbox", "mylar3", "folder", "comicinfo", "provider")
_LIFECYCLES = ("active", "archived")
_RESOLUTION_STATES = ("pending", "resolved", "missing", "ambiguous", "conflict", "skipped")
_IMPORTED_STATUSES = (
    "detected",
    "needs_review",
    "ready",
    "confirmed",
    "skipped",
    "imported",
    "failed",
)
_MATERIALIZATION_MODES = ("copy", "hardlink", "symlink", "reference_only")
_PLACEMENT_OWNERS = ("managed", "referenced")
_SYMLINK_STYLES = ("absolute", "relative")
_PLACEMENT_STATES = ("current", "missing", "drifted", "failed")


def _enum(values: tuple[str, ...], *, name: str) -> sa.Enum:
    return sa.Enum(
        *values,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
    )


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def _add_story_arc_enum_columns() -> None:
    if _is_sqlite():
        op.execute(
            sa.text(
                "ALTER TABLE story_arcs ADD COLUMN source_kind VARCHAR(9) "
                "NOT NULL DEFAULT 'legacy' CONSTRAINT storyarcsourcekind "
                "CHECK (source_kind IN "
                "('legacy','pullbox','mylar3','folder','comicinfo','provider'))"
            )
        )
        op.execute(
            sa.text(
                "ALTER TABLE story_arcs ADD COLUMN lifecycle VARCHAR(8) "
                "NOT NULL DEFAULT 'active' CONSTRAINT storyarclifecycle "
                "CHECK (lifecycle IN ('active','archived'))"
            )
        )
        return

    op.add_column(
        "story_arcs",
        sa.Column("source_kind", sa.String(9), nullable=False, server_default="legacy"),
    )
    op.create_check_constraint(
        "storyarcsourcekind",
        "story_arcs",
        "source_kind IN ('legacy','pullbox','mylar3','folder','comicinfo','provider')",
    )
    op.add_column(
        "story_arcs",
        sa.Column("lifecycle", sa.String(8), nullable=False, server_default="active"),
    )
    op.create_check_constraint(
        "storyarclifecycle",
        "story_arcs",
        "lifecycle IN ('active','archived')",
    )


def _add_story_arc_foreign_key_columns() -> None:
    if _is_sqlite():
        op.execute(
            sa.text(
                "ALTER TABLE story_arcs ADD COLUMN target_library_root_id INTEGER "
                "CONSTRAINT fk_story_arcs_target_library_root_id_library_roots "
                "REFERENCES library_roots(id) ON DELETE SET NULL"
            )
        )
        op.execute(
            sa.text(
                "ALTER TABLE story_arcs ADD COLUMN source_import_job_id INTEGER "
                "CONSTRAINT fk_story_arcs_source_import_job_id_import_jobs "
                "REFERENCES import_jobs(id) ON DELETE SET NULL"
            )
        )
        return

    op.add_column(
        "story_arcs",
        sa.Column("target_library_root_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_story_arcs_target_library_root_id_library_roots",
        "story_arcs",
        "library_roots",
        ["target_library_root_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "story_arcs",
        sa.Column("source_import_job_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_story_arcs_source_import_job_id_import_jobs",
        "story_arcs",
        "import_jobs",
        ["source_import_job_id"],
        ["id"],
        ondelete="SET NULL",
    )


def _normalize_story_arc_name(value: object) -> str:
    normalized = unicodedata.normalize("NFC", str(value)).casefold()
    return " ".join(normalized.split())


def _backfill_normalized_story_arc_names() -> None:
    connection = op.get_bind()
    last_id = 0
    select_batch = sa.text(
        "SELECT id, name FROM story_arcs WHERE id > :last_id ORDER BY id LIMIT :batch_size"
    )
    update_row = sa.text(
        "UPDATE story_arcs SET normalized_name = :normalized_name WHERE id = :story_arc_id"
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
        connection.execute(
            update_row,
            [
                {
                    "story_arc_id": int(row["id"]),
                    "normalized_name": _normalize_story_arc_name(row["name"]),
                }
                for row in rows
            ],
        )
        last_id = int(rows[-1]["id"])


def _add_story_arc_columns() -> None:
    op.add_column(
        "story_arcs",
        sa.Column(
            "normalized_name",
            sa.String(500),
            nullable=False,
            server_default="__legacy__",
        ),
    )
    _add_story_arc_enum_columns()
    for column_name in ("monitored", "search_missing", "include_upcoming", "sync_enabled"):
        op.add_column(
            "story_arcs",
            sa.Column(
                column_name,
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
    _add_story_arc_foreign_key_columns()
    op.add_column(
        "story_arcs",
        sa.Column("policy_schema_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "story_arcs",
        sa.Column("policy_snapshot", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "story_arcs",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "story_arcs",
        sa.Column("diagnostics", sa.JSON(), nullable=False, server_default="{}"),
    )
    _backfill_normalized_story_arc_names()

    op.create_index(
        "ix_story_arcs_normalized_id",
        "story_arcs",
        ["normalized_name", "id"],
        unique=False,
    )
    op.create_index(
        "ix_story_arcs_lifecycle_monitored_id",
        "story_arcs",
        ["lifecycle", "monitored", "id"],
        unique=False,
    )
    op.create_index(
        "ix_story_arcs_source_job_id",
        "story_arcs",
        ["source_import_job_id", "id"],
        unique=False,
    )


def _create_issue_story_arcs() -> None:
    op.create_table(
        "issue_story_arcs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("story_arc_id", sa.Integer(), nullable=False),
        sa.Column("issue_id", sa.Integer(), nullable=True),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("source_ordinal", sa.Integer(), nullable=False),
        sa.Column(
            "legacy_sequence_was_null",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "resolution_state",
            _enum(_RESOLUTION_STATES, name="storyarcresolutionstate"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "source_kind",
            _enum(_SOURCE_KINDS, name="storyarcsourcekind"),
            nullable=False,
            server_default="legacy",
        ),
        sa.Column("source_entry_id", sa.String(255), nullable=True),
        sa.Column("source_arc_id", sa.String(255), nullable=True),
        sa.Column("source_issue_id", sa.String(255), nullable=True),
        sa.Column("source_series_id", sa.String(255), nullable=True),
        sa.Column("source_issue_number_text", sa.String(320), nullable=True),
        sa.Column("source_series_name", sa.String(500), nullable=True),
        sa.Column("source_issue_title", sa.String(500), nullable=True),
        sa.Column("source_publisher", sa.String(255), nullable=True),
        sa.Column("source_release_date_text", sa.String(50), nullable=True),
        sa.Column("source_issue_date_text", sa.String(50), nullable=True),
        sa.Column("resolution_confidence", sa.Float(), nullable=True),
        sa.Column("resolution_method", sa.String(50), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "sync_eligible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "last_materialization_result",
            sa.JSON(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["story_arc_id"],
            ["story_arcs.id"],
            name="fk_issue_story_arcs_story_arc_id_story_arcs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["issue_id"],
            ["issues.id"],
            name="fk_issue_story_arcs_issue_id_issues",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_issue_story_arcs_v2"),
        sa.UniqueConstraint(
            "story_arc_id",
            "issue_id",
            name="uq_issue_story_arcs_arc_issue",
        ),
    )
    op.create_index(
        "ix_issue_story_arcs_order",
        "issue_story_arcs",
        ["story_arc_id", "sequence_number", "source_ordinal", "id"],
        unique=False,
    )
    op.create_index(
        "ix_issue_story_arcs_review",
        "issue_story_arcs",
        ["story_arc_id", "resolution_state", "sequence_number", "source_ordinal", "id"],
        unique=False,
    )
    op.create_index(
        "ix_issue_story_arcs_issue",
        "issue_story_arcs",
        ["issue_id", "story_arc_id", "id"],
        unique=False,
    )


def _rebuild_issue_story_arcs_for_upgrade() -> None:
    op.rename_table("issue_story_arcs", "issue_story_arcs_legacy")
    _create_issue_story_arcs()

    connection = op.get_bind()
    connection.execute(
        sa.text(
            "WITH arc_max AS ("
            "SELECT story_arc_id, COALESCE(MAX(sequence_number), 0) AS max_sequence "
            "FROM issue_story_arcs_legacy GROUP BY story_arc_id"
            "), assigned AS ("
            "SELECT legacy.story_arc_id, legacy.issue_id, legacy.sequence_number, "
            "CASE WHEN legacy.sequence_number IS NULL THEN "
            "arc_max.max_sequence + ROW_NUMBER() OVER ("
            "PARTITION BY legacy.story_arc_id, "
            "CASE WHEN legacy.sequence_number IS NULL THEN 1 ELSE 0 END "
            "ORDER BY legacy.issue_id) ELSE legacy.sequence_number END AS assigned_sequence "
            "FROM issue_story_arcs_legacy AS legacy "
            "JOIN arc_max ON arc_max.story_arc_id = legacy.story_arc_id"
            "), ranked AS ("
            "SELECT assigned.*, ROW_NUMBER() OVER ("
            "PARTITION BY assigned.story_arc_id "
            "ORDER BY assigned.assigned_sequence, assigned.issue_id) AS assigned_ordinal "
            "FROM assigned"
            ") "
            "INSERT INTO issue_story_arcs ("
            "story_arc_id, issue_id, sequence_number, source_ordinal, "
            "legacy_sequence_was_null, resolution_state, source_kind, "
            "source_issue_number_text, evidence, sync_eligible, "
            "last_materialization_result, created_at, updated_at) "
            "SELECT ranked.story_arc_id, ranked.issue_id, ranked.assigned_sequence, "
            "ranked.assigned_ordinal, "
            "CASE WHEN ranked.sequence_number IS NULL THEN TRUE ELSE FALSE END, "
            "'resolved', 'legacy', "
            "issues.issue_number_text, '{}', FALSE, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
            "FROM ranked JOIN issues ON issues.id = ranked.issue_id "
            "ORDER BY ranked.story_arc_id, ranked.assigned_ordinal"
        )
    )
    op.drop_table("issue_story_arcs_legacy")


def _create_story_arc_external_identities() -> None:
    op.create_table(
        "story_arc_external_identities",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("story_arc_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("namespace", sa.String(100), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("source_url", sa.String(1000), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["story_arc_id"],
            ["story_arcs.id"],
            name="fk_story_arc_external_identities_story_arc_id_story_arcs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_story_arc_external_identities"),
        sa.UniqueConstraint(
            "source",
            "namespace",
            "external_id",
            name="uq_story_arc_external_identity",
        ),
    )
    op.create_index(
        "ix_story_arc_external_identities_arc_id",
        "story_arc_external_identities",
        ["story_arc_id", "id"],
        unique=False,
    )
    op.get_bind().execute(
        sa.text(
            "INSERT INTO story_arc_external_identities "
            "(story_arc_id, source, namespace, external_id, evidence, created_at, updated_at) "
            "SELECT id, 'comicvine', 'story_arc', CAST(comicvine_id AS VARCHAR(255)), "
            "'{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP FROM story_arcs "
            "WHERE comicvine_id IS NOT NULL"
        )
    )


def _create_import_story_arc_tables() -> None:
    op.create_table(
        "import_story_arcs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("import_job_id", sa.Integer(), nullable=False),
        sa.Column(
            "source_kind",
            _enum(_SOURCE_KINDS, name="storyarcsourcekind"),
            nullable=False,
        ),
        sa.Column("source_key", sa.String(255), nullable=False),
        sa.Column("source_arc_id", sa.String(255), nullable=True),
        sa.Column("source_ordinal", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(500), nullable=True),
        sa.Column("normalized_name", sa.String(500), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status",
            _enum(_IMPORTED_STATUSES, name="importedstoryarcstatus"),
            nullable=False,
            server_default="detected",
        ),
        sa.Column(
            "selected_for_import",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("proposed_story_arc_id", sa.Integer(), nullable=True),
        sa.Column("materialized_story_arc_id", sa.Integer(), nullable=True),
        sa.Column(
            "proposed_policy_snapshot",
            sa.JSON(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "source_settings_snapshot",
            sa.JSON(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("diagnostics", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["import_job_id"],
            ["import_jobs.id"],
            name="fk_import_story_arcs_import_job_id_import_jobs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["proposed_story_arc_id"],
            ["story_arcs.id"],
            name="fk_import_story_arcs_proposed_story_arc_id_story_arcs",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["materialized_story_arc_id"],
            ["story_arcs.id"],
            name="fk_import_story_arcs_materialized_story_arc_id_story_arcs",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_import_story_arcs"),
        sa.UniqueConstraint(
            "import_job_id",
            "source_key",
            name="uq_import_story_arcs_job_source_key",
        ),
    )
    op.create_index(
        "ix_import_story_arcs_job_status_id",
        "import_story_arcs",
        ["import_job_id", "status", "id"],
        unique=False,
    )
    op.create_index(
        "ix_import_story_arcs_job_normalized_id",
        "import_story_arcs",
        ["import_job_id", "normalized_name", "id"],
        unique=False,
    )

    op.create_table(
        "import_story_arc_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("imported_story_arc_id", sa.Integer(), nullable=False),
        sa.Column("import_file_id", sa.Integer(), nullable=True),
        sa.Column("matched_issue_id", sa.Integer(), nullable=True),
        sa.Column("materialized_membership_id", sa.Integer(), nullable=True),
        sa.Column("source_ordinal", sa.Integer(), nullable=False),
        sa.Column("reading_order", sa.Integer(), nullable=True),
        sa.Column("reading_order_raw", sa.String(50), nullable=True),
        sa.Column(
            "resolution_state",
            _enum(_RESOLUTION_STATES, name="storyarcresolutionstate"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "source_kind",
            _enum(_SOURCE_KINDS, name="storyarcsourcekind"),
            nullable=False,
        ),
        sa.Column("source_entry_id", sa.String(255), nullable=True),
        sa.Column("source_arc_id", sa.String(255), nullable=True),
        sa.Column("source_issue_id", sa.String(255), nullable=True),
        sa.Column("source_series_id", sa.String(255), nullable=True),
        sa.Column("source_issue_number_text", sa.String(320), nullable=True),
        sa.Column("source_series_name", sa.String(500), nullable=True),
        sa.Column("source_issue_title", sa.String(500), nullable=True),
        sa.Column("source_publisher", sa.String(255), nullable=True),
        sa.Column("source_release_date_text", sa.String(50), nullable=True),
        sa.Column("source_issue_date_text", sa.String(50), nullable=True),
        sa.Column("resolution_confidence", sa.Float(), nullable=True),
        sa.Column("resolution_method", sa.String(50), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("source_location", sa.String(1000), nullable=True),
        sa.Column(
            "selected_for_import",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("diagnostics", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["imported_story_arc_id"],
            ["import_story_arcs.id"],
            name="fk_import_story_arc_entries_imported_story_arc_id_import_story_arcs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["import_file_id"],
            ["import_files.id"],
            name="fk_import_story_arc_entries_import_file_id_import_files",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["matched_issue_id"],
            ["issues.id"],
            name="fk_import_story_arc_entries_matched_issue_id_issues",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["materialized_membership_id"],
            ["issue_story_arcs.id"],
            name="fk_import_story_arc_entries_materialized_membership_id_issue_story_arcs",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_import_story_arc_entries"),
        sa.UniqueConstraint(
            "imported_story_arc_id",
            "source_ordinal",
            name="uq_import_story_arc_entries_arc_ordinal",
        ),
    )
    op.create_index(
        "ix_import_story_arc_entries_arc_resolution_order",
        "import_story_arc_entries",
        [
            "imported_story_arc_id",
            "resolution_state",
            "reading_order",
            "source_ordinal",
            "id",
        ],
        unique=False,
    )
    op.create_index(
        "ix_import_story_arc_entries_matched_issue_id",
        "import_story_arc_entries",
        ["matched_issue_id", "id"],
        unique=False,
    )


def _create_story_arc_placements() -> None:
    op.create_table(
        "story_arc_placements",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("issue_story_arc_id", sa.Integer(), nullable=False),
        sa.Column("library_file_id", sa.Integer(), nullable=True),
        sa.Column("library_root_id", sa.Integer(), nullable=True),
        sa.Column("placement_path", sa.String(1000), nullable=False),
        sa.Column(
            "mode",
            _enum(_MATERIALIZATION_MODES, name="storyarcplacementmode"),
            nullable=False,
            server_default="reference_only",
        ),
        sa.Column(
            "ownership",
            _enum(_PLACEMENT_OWNERS, name="storyarcplacementownership"),
            nullable=False,
            server_default="referenced",
        ),
        sa.Column(
            "symlink_style",
            _enum(_SYMLINK_STYLES, name="storyarcsymlinkstyle"),
            nullable=True,
        ),
        sa.Column(
            "source_kind",
            _enum(_SOURCE_KINDS, name="storyarcsourcekind"),
            nullable=False,
            server_default="legacy",
        ),
        sa.Column("source_import_job_id", sa.Integer(), nullable=True),
        sa.Column("creating_action_id", sa.Integer(), nullable=True),
        sa.Column("rendered_reading_order", sa.Integer(), nullable=True),
        sa.Column("policy_schema_version", sa.Integer(), nullable=True),
        sa.Column("source_fingerprint", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "state",
            _enum(_PLACEMENT_STATES, name="storyarcplacementstate"),
            nullable=False,
            server_default="current",
        ),
        sa.Column("last_result", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "mode = 'reference_only'",
            name="ck_story_arc_placements_reference_only_mode",
        ),
        sa.CheckConstraint(
            "ownership = 'referenced'",
            name="ck_story_arc_placements_reference_only_owner",
        ),
        sa.CheckConstraint(
            "symlink_style IS NULL",
            name="ck_story_arc_placements_no_symlink_style",
        ),
        sa.ForeignKeyConstraint(
            ["issue_story_arc_id"],
            ["issue_story_arcs.id"],
            name="fk_story_arc_placements_issue_story_arc_id_issue_story_arcs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["library_file_id"],
            ["library_files.id"],
            name="fk_story_arc_placements_library_file_id_library_files",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["library_root_id"],
            ["library_roots.id"],
            name="fk_story_arc_placements_library_root_id_library_roots",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_import_job_id"],
            ["import_jobs.id"],
            name="fk_story_arc_placements_source_import_job_id_import_jobs",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["creating_action_id"],
            ["import_job_actions.id"],
            name="fk_story_arc_placements_creating_action_id_import_job_actions",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_story_arc_placements"),
        sa.UniqueConstraint("placement_path", name="uq_story_arc_placements_path"),
    )
    op.create_index(
        "ix_story_arc_placements_membership",
        "story_arc_placements",
        ["issue_story_arc_id", "id"],
        unique=False,
    )
    op.create_index(
        "ix_story_arc_placements_library_file",
        "story_arc_placements",
        ["library_file_id", "id"],
        unique=False,
    )
    op.create_index(
        "ix_story_arc_placements_state",
        "story_arc_placements",
        ["state", "id"],
        unique=False,
    )


def _assert_downgrade_is_lossless() -> None:
    connection = op.get_bind()
    staged = connection.execute(sa.text("SELECT id FROM import_story_arcs LIMIT 1")).first()
    if staged is not None:
        raise RuntimeError(
            "Cannot downgrade to the legacy story-arc schema while staged imports remain."
        )
    placement = connection.execute(sa.text("SELECT id FROM story_arc_placements LIMIT 1")).first()
    if placement is not None:
        raise RuntimeError(
            "Cannot downgrade to the legacy story-arc schema while arc placements remain."
        )
    cohort = connection.execute(
        sa.text(
            "SELECT id FROM import_files WHERE source_folder_cohort_key IS NOT NULL "
            "OR source_ordinal IS NOT NULL LIMIT 1"
        )
    ).first()
    if cohort is not None:
        raise RuntimeError(
            "Cannot downgrade to the legacy story-arc schema while folder cohort data remains."
        )
    external = connection.execute(
        sa.text(
            "SELECT external.id FROM story_arc_external_identities AS external "
            "JOIN story_arcs AS arc ON arc.id = external.story_arc_id "
            "WHERE external.source != 'comicvine' "
            "OR external.namespace != 'story_arc' "
            "OR arc.comicvine_id IS NULL "
            "OR external.external_id != CAST(arc.comicvine_id AS VARCHAR(255)) LIMIT 1"
        )
    ).first()
    if external is not None:
        raise RuntimeError(
            "Cannot downgrade to the legacy story-arc schema while external identities "
            "lack a legacy ComicVine mirror."
        )
    nonlegacy_arc = connection.execute(
        sa.text(
            "SELECT id FROM story_arcs WHERE source_kind != 'legacy' "
            "OR lifecycle != 'active' OR monitored IS TRUE OR search_missing IS TRUE "
            "OR include_upcoming IS TRUE OR sync_enabled IS TRUE "
            "OR target_library_root_id IS NOT NULL OR policy_schema_version IS NOT NULL "
            "OR CAST(policy_snapshot AS TEXT) != '{}' OR source_import_job_id IS NOT NULL "
            "OR revision != 1 OR CAST(diagnostics AS TEXT) != '{}' LIMIT 1"
        )
    ).first()
    if nonlegacy_arc is not None:
        raise RuntimeError(
            "Cannot downgrade to the legacy story-arc schema while first-class arc policy "
            "or monitoring data remains."
        )
    nonlegacy_membership = connection.execute(
        sa.text(
            "SELECT membership.id FROM issue_story_arcs AS membership "
            "LEFT JOIN issues ON issues.id = membership.issue_id "
            "WHERE membership.issue_id IS NULL "
            "OR membership.resolution_state != 'resolved' "
            "OR membership.source_kind != 'legacy' "
            "OR membership.source_entry_id IS NOT NULL "
            "OR membership.source_arc_id IS NOT NULL "
            "OR membership.source_issue_id IS NOT NULL "
            "OR membership.source_series_id IS NOT NULL "
            "OR (membership.source_issue_number_text IS NOT NULL AND "
            "membership.source_issue_number_text != issues.issue_number_text) "
            "OR membership.source_series_name IS NOT NULL "
            "OR membership.source_issue_title IS NOT NULL "
            "OR membership.source_publisher IS NOT NULL "
            "OR membership.source_release_date_text IS NOT NULL "
            "OR membership.source_issue_date_text IS NOT NULL "
            "OR membership.resolution_confidence IS NOT NULL "
            "OR membership.resolution_method IS NOT NULL "
            "OR CAST(membership.evidence AS TEXT) != '{}' "
            "OR membership.sync_eligible IS TRUE "
            "OR CAST(membership.last_materialization_result AS TEXT) != '{}' LIMIT 1"
        )
    ).first()
    if nonlegacy_membership is not None:
        raise RuntimeError(
            "Cannot downgrade to the legacy story-arc schema while unresolved or "
            "provenance-rich memberships remain."
        )
    nondeterministic_ordinal = connection.execute(
        sa.text(
            "SELECT ranked.id FROM ("
            "SELECT membership.id, membership.source_ordinal, "
            "ROW_NUMBER() OVER (PARTITION BY membership.story_arc_id "
            "ORDER BY membership.sequence_number, membership.issue_id) AS expected_ordinal "
            "FROM issue_story_arcs AS membership"
            ") AS ranked WHERE ranked.source_ordinal != ranked.expected_ordinal LIMIT 1"
        )
    ).first()
    if nondeterministic_ordinal is not None:
        raise RuntimeError(
            "Cannot downgrade to the legacy story-arc schema while custom membership "
            "tie-break ordering remains."
        )


def _rebuild_issue_story_arcs_for_downgrade() -> None:
    op.rename_table("issue_story_arcs", "issue_story_arcs_v2")
    op.create_table(
        "issue_story_arcs",
        sa.Column("issue_id", sa.Integer(), nullable=False),
        sa.Column("story_arc_id", sa.Integer(), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["issue_id"],
            ["issues.id"],
            name="fk_issue_story_arcs_issue_id_issues_legacy",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["story_arc_id"],
            ["story_arcs.id"],
            name="fk_issue_story_arcs_story_arc_id_story_arcs_legacy",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "issue_id",
            "story_arc_id",
            name="pk_issue_story_arcs_legacy_restore",
        ),
    )
    op.get_bind().execute(
        sa.text(
            "INSERT INTO issue_story_arcs (issue_id, story_arc_id, sequence_number) "
            "SELECT issue_id, story_arc_id, "
            "CASE WHEN legacy_sequence_was_null THEN NULL ELSE sequence_number END "
            "FROM issue_story_arcs_v2 ORDER BY story_arc_id, issue_id"
        )
    )
    op.drop_table("issue_story_arcs_v2")


def _drop_story_arc_columns() -> None:
    op.drop_index("ix_story_arcs_source_job_id", table_name="story_arcs")
    op.drop_index("ix_story_arcs_lifecycle_monitored_id", table_name="story_arcs")
    op.drop_index("ix_story_arcs_normalized_id", table_name="story_arcs")

    if not _is_sqlite():
        op.drop_constraint(
            "fk_story_arcs_source_import_job_id_import_jobs",
            "story_arcs",
            type_="foreignkey",
        )
        op.drop_constraint(
            "fk_story_arcs_target_library_root_id_library_roots",
            "story_arcs",
            type_="foreignkey",
        )
        op.drop_constraint("storyarclifecycle", "story_arcs", type_="check")
        op.drop_constraint("storyarcsourcekind", "story_arcs", type_="check")

    for column_name in (
        "diagnostics",
        "revision",
        "policy_snapshot",
        "policy_schema_version",
        "source_import_job_id",
        "target_library_root_id",
        "sync_enabled",
        "include_upcoming",
        "search_missing",
        "monitored",
        "lifecycle",
        "source_kind",
        "normalized_name",
    ):
        op.drop_column("story_arcs", column_name)


def upgrade() -> None:
    """Add first-class arc identity, import staging, and reference-only placements."""
    _add_story_arc_columns()
    op.add_column(
        "import_files",
        sa.Column("source_folder_cohort_key", sa.String(1000), nullable=True),
    )
    op.add_column(
        "import_files",
        sa.Column("source_ordinal", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_import_files_job_cohort_order",
        "import_files",
        ["import_job_id", "source_folder_cohort_key", "source_ordinal", "id"],
        unique=False,
    )
    _rebuild_issue_story_arcs_for_upgrade()
    _create_story_arc_external_identities()
    _create_import_story_arc_tables()
    _create_story_arc_placements()


def downgrade() -> None:
    """Restore the legacy association only when all new data is representable."""
    _assert_downgrade_is_lossless()

    op.drop_table("story_arc_placements")
    op.drop_table("import_story_arc_entries")
    op.drop_table("import_story_arcs")
    op.drop_table("story_arc_external_identities")
    _rebuild_issue_story_arcs_for_downgrade()

    op.drop_index("ix_import_files_job_cohort_order", table_name="import_files")
    op.drop_column("import_files", "source_ordinal")
    op.drop_column("import_files", "source_folder_cohort_key")
    _drop_story_arc_columns()
