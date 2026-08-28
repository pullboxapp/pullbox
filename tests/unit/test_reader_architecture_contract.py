"""Static boundaries that keep reading state reusable by future adapters."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import TYPE_CHECKING

from pullbox.models.issue import Issue
from pullbox.models.series import Series
from pullbox.services import reader_state_service, reading_query_service

if TYPE_CHECKING:
    from types import ModuleType


def _import_roots(module: ModuleType) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    return roots


def test_reader_domain_services_do_not_depend_on_web_or_template_adapters() -> None:
    forbidden_prefixes = ("fastapi", "jinja2", "pullbox.ui", "pullbox.api")

    for module in (reader_state_service, reading_query_service):
        imports = _import_roots(module)
        assert not any(
            imported == prefix or imported.startswith(f"{prefix}.")
            for imported in imports
            for prefix in forbidden_prefixes
        )


def test_catalog_models_do_not_own_private_reading_dimensions() -> None:
    forbidden_columns = {
        "last_page_index",
        "completed_at",
        "want_to_read",
        "reading_state",
    }

    assert forbidden_columns.isdisjoint(Issue.__table__.columns.keys())
    assert forbidden_columns.isdisjoint(Series.__table__.columns.keys())


def test_reader_feature_does_not_create_a_parallel_opds_state_implementation() -> None:
    services_dir = Path(inspect.getfile(reader_state_service)).parent
    opds_reader_files = tuple(services_dir.glob("*opds*reader*"))

    assert opds_reader_files == ()
