"""Add explicit multi-library root roles and managed default.

Revision ID: h9c0d1e2f345
Revises: g8b9c0d1e234
Create Date: 2026-08-31
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "h9c0d1e2f345"
down_revision: str | Sequence[str] | None = "g8b9c0d1e234"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_INDEX = "uq_library_roots_default_managed_destination"
_LEGACY_DEFAULT_ROOT = sa.text(
    "SELECT roots.id FROM library_roots AS roots "
    "LEFT JOIN system_config AS config "
    "ON config.key = 'comics_directory' AND config.value = roots.path "
    "WHERE roots.enabled = true AND roots.allow_managed_writes = true "
    "ORDER BY CASE WHEN config.key IS NULL THEN 1 ELSE 0 END, roots.id "
    "LIMIT 1"
)


def upgrade() -> None:
    """Add root roles, backfill one legacy default, and constrain uniqueness."""
    op.add_column(
        "library_roots",
        sa.Column(
            "allow_referenced_registrations",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "library_roots",
        sa.Column(
            "allow_managed_writes",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "library_roots",
        sa.Column(
            "is_default_managed_destination",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    bind = op.get_bind()
    selected_id = bind.execute(_LEGACY_DEFAULT_ROOT).scalar_one_or_none()
    if selected_id is not None:
        bind.execute(
            sa.text(
                "UPDATE library_roots SET is_default_managed_destination = true WHERE id = :root_id"
            ),
            {"root_id": selected_id},
        )

    op.create_index(
        _DEFAULT_INDEX,
        "library_roots",
        ["is_default_managed_destination"],
        unique=True,
        sqlite_where=sa.text("is_default_managed_destination = 1"),
        postgresql_where=sa.text("is_default_managed_destination"),
    )


def downgrade() -> None:
    """Remove explicit root-management fields and the default constraint."""
    bind = op.get_bind()
    nonrepresentable_roles = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM library_roots "
            "WHERE allow_referenced_registrations = false "
            "OR allow_managed_writes = false"
        )
    ).scalar_one()
    if nonrepresentable_roles:
        raise RuntimeError(
            "Cannot downgrade library-root capabilities while a root disables "
            "referenced registrations or managed writes."
        )

    selected_id = bind.execute(_LEGACY_DEFAULT_ROOT).scalar_one_or_none()
    current_default_id = bind.execute(
        sa.text("SELECT id FROM library_roots WHERE is_default_managed_destination = true")
    ).scalar_one_or_none()
    if current_default_id != selected_id:
        raise RuntimeError(
            "Cannot downgrade library-root capabilities because the default managed "
            "destination cannot be reconstructed by the legacy schema."
        )

    op.drop_index(_DEFAULT_INDEX, table_name="library_roots")
    op.drop_column("library_roots", "is_default_managed_destination")
    op.drop_column("library_roots", "allow_managed_writes")
    op.drop_column("library_roots", "allow_referenced_registrations")
