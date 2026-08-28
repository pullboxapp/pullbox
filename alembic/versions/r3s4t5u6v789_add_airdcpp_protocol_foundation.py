"""Add AirDC++ protocol and exact-client history foundation.

Revision ID: r3s4t5u6v789
Revises: q2r3s4t5u678
Create Date: 2026-08-25
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "r3s4t5u6v789"
down_revision: str | Sequence[str] | None = "q2r3s4t5u678"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROTOCOL_BY_CLIENT = {
    "SABNZBD": "usenet",
    "NZBGET": "usenet",
    "QBITTORRENT": "torrent",
    "TRANSMISSION": "torrent",
    "DELUGE": "torrent",
    "DIRECT": "direct",
    "AIRDCPP": "dc",
}


def _protocol_enum() -> sa.Enum:
    return sa.Enum(
        "usenet",
        "torrent",
        "direct",
        "dc",
        name="acquisitionprotocol",
        native_enum=False,
        create_constraint=True,
    )


def _update_source_priority(*, add_dc: bool) -> None:
    if op.get_context().as_sql:
        op.execute("-- source_priority JSON compatibility is applied by the online migration")
        return

    bind = op.get_bind()
    value = bind.execute(
        sa.text("SELECT value FROM system_config WHERE key = 'source_priority'")
    ).scalar_one_or_none()
    if not isinstance(value, str):
        return
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return
    if not isinstance(parsed, list):
        return

    if add_dc:
        if "dc" not in parsed:
            parsed.append("dc")
    else:
        parsed = [item for item in parsed if item != "dc"]

    bind.execute(
        sa.text("UPDATE system_config SET value = :value WHERE key = 'source_priority'"),
        {"value": json.dumps(parsed)},
    )


def upgrade() -> None:
    """Add and backfill protocol plus nullable exact-client identity."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE downloadclienttype ADD VALUE IF NOT EXISTS 'AIRDCPP'")

    with op.batch_alter_table("download_history") as batch_op:
        batch_op.add_column(sa.Column("protocol", _protocol_enum(), nullable=True))
        batch_op.add_column(sa.Column("download_client_config_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_download_history_client_config",
            "download_client_configs",
            ["download_client_config_id"],
            ["id"],
            ondelete="SET NULL",
        )

    cases = " ".join(
        f"WHEN '{client_type}' THEN '{protocol}'"
        for client_type, protocol in _PROTOCOL_BY_CLIENT.items()
    )
    op.execute(
        sa.text(
            f"UPDATE download_history SET protocol = CASE download_client {cases} ELSE NULL END"
        )
    )

    with op.batch_alter_table("download_history") as batch_op:
        batch_op.alter_column(
            "protocol",
            existing_type=_protocol_enum(),
            nullable=False,
        )

    op.create_index(
        "ix_download_history_protocol_state",
        "download_history",
        ["protocol", "state"],
        unique=False,
    )
    op.create_index(
        "ix_download_history_client_config_state",
        "download_history",
        ["download_client_config_id", "state"],
        unique=False,
    )
    _update_source_priority(add_dc=True)


def downgrade() -> None:
    """Remove the foundation only when no AirDC++ history would be lost."""
    bind = op.get_bind()
    if op.get_context().as_sql:
        op.execute("-- verify no AirDC++ download history exists before downgrade")
    else:
        airdcpp_rows = bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM download_history "
                "WHERE download_client = 'AIRDCPP' OR protocol = 'dc'"
            )
        ).scalar_one()
        if airdcpp_rows:
            raise RuntimeError("Cannot downgrade while AirDC++ download history exists")

    _update_source_priority(add_dc=False)
    op.drop_index("ix_download_history_client_config_state", table_name="download_history")
    op.drop_index("ix_download_history_protocol_state", table_name="download_history")
    with op.batch_alter_table("download_history") as batch_op:
        batch_op.drop_constraint("fk_download_history_client_config", type_="foreignkey")
        batch_op.drop_column("download_client_config_id")
        batch_op.drop_column("protocol", existing_type=_protocol_enum())

    # PostgreSQL enum values cannot be removed safely. AIRDCPP remains dormant
    # after downgrade; the prior ORM has no path that can write it.
