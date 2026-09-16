"""Import-workflow dropdown contract coverage."""

from __future__ import annotations

from pathlib import Path

import pytest

IMPORT_TEMPLATE_ROOT = Path("src/pullbox/ui/templates")
IMPORT_WORKFLOW_TEMPLATES = sorted(
    [
        IMPORT_TEMPLATE_ROOT / "pages/import.html",
        *(IMPORT_TEMPLATE_ROOT / "partials").glob("import*.html"),
        IMPORT_TEMPLATE_ROOT / "partials/issue_import_progress_modal.html",
    ]
)


@pytest.mark.parametrize(
    "template_path",
    IMPORT_WORKFLOW_TEMPLATES,
    ids=lambda path: path.name,
)
def test_import_workflow_has_no_native_dropdowns(template_path: Path) -> None:
    """Every import screen must use the shared dropdown-select contract."""
    template = template_path.read_text(encoding="utf-8")

    assert "<select" not in template, (
        f"{template_path} contains a native select instead of dropdown_select()."
    )
