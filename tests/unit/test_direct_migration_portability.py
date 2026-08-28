from __future__ import annotations

import importlib.util
import io
from pathlib import Path
from typing import TYPE_CHECKING

from alembic.migration import MigrationContext
from alembic.operations import Operations

if TYPE_CHECKING:
    from types import ModuleType


def _postgresql_upgrade_sql(module_name: str) -> str:
    output = io.StringIO()
    context = MigrationContext.configure(
        url="postgresql://",
        opts={"as_sql": True, "output_buffer": output},
    )
    module = _load_migration(module_name)
    original_op = module.op
    try:
        module.op = Operations(context)
        module.upgrade()
    finally:
        module.op = original_op
    return output.getvalue()


def _load_migration(module_name: str) -> ModuleType:
    path = Path(__file__).parents[2] / "alembic" / "versions" / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load migration {module_name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manual_resolver_opt_in_uses_postgresql_boolean_default() -> None:
    sql = _postgresql_upgrade_sql("i4e5f6g70829_add_manual_torznab_resolver_opt_in")

    assert "resolver_enabled BOOLEAN DEFAULT false NOT NULL" in sql


def test_manager_availability_uses_postgresql_boolean_default() -> None:
    sql = _postgresql_upgrade_sql("j5f6g7h81930_add_indexer_manager_identity")

    assert "manager_available BOOLEAN DEFAULT true NOT NULL" in sql


def test_airdcpp_foundation_renders_portable_postgresql_schema() -> None:
    sql = _postgresql_upgrade_sql("r3s4t5u6v789_add_airdcpp_protocol_foundation")

    assert "ALTER TYPE downloadclienttype ADD VALUE IF NOT EXISTS 'AIRDCPP'" in sql
    assert "ADD COLUMN protocol VARCHAR(7)" in sql
    assert "protocol IN ('usenet', 'torrent', 'direct', 'dc')" in sql
    assert "ALTER COLUMN protocol SET NOT NULL" in sql
    assert "FOREIGN KEY(download_client_config_id)" in sql
    assert "REFERENCES download_client_configs (id) ON DELETE SET NULL" in sql


def test_airdcpp_settings_render_portable_postgresql_schema() -> None:
    sql = _postgresql_upgrade_sql("s4t5u6v7w890_add_airdcpp_client_settings")

    assert "CREATE TABLE airdcpp_client_settings" in sql
    assert "FOREIGN KEY(client_config_id)" in sql
    assert "REFERENCES download_client_configs (id) ON DELETE CASCADE" in sql
    assert "minimum_search_interval_seconds BETWEEN 45 AND 3600" in sql
    assert "max_retained_routes BETWEEN max_results AND 2000" in sql
