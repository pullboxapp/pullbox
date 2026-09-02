"""Static XSS contracts for template and inline JavaScript sinks."""

from __future__ import annotations

from pathlib import Path

TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "src" / "pullbox" / "ui" / "templates"

# `|safe` must stay limited to reviewed internal slots or sanitized helpers.
SAFE_FILTER_ALLOWLIST = {
    (
        "components/mission_control_workspace.html",
        "{{ header_left_html | safe }}",
    ),
    (
        "components/mission_control_workspace.html",
        "{{ header_actions_html | safe }}",
    ),
    (
        "components/mission_control_workspace.html",
        "{{ toolbar_attrs | safe }}",
    ),
    (
        "components/mission_control_workspace.html",
        "{{ hidden_inputs_html | safe }}",
    ),
    (
        "components/mission_control_workspace.html",
        "{{ browse_toolbar_html | safe }}",
    ),
    (
        "components/mission_control_workspace.html",
        "{{ select_toolbar_html | safe }}",
    ),
    (
        "components/mission_control_workspace.html",
        "{{ results_body_attrs | safe }}",
    ),
    (
        "components/mission_control_workspace.html",
        "{{ results_html | safe }}",
    ),
    (
        "components/page_context.html",
        '<header class="page-context-card {{ class_name }}" {{ attrs|safe }}>',
    ),
    (
        "components/page_context.html",
        '<p class="page-context-summary">{{ summary|safe }}</p>',
    ),
    (
        "components/page_context.html",
        '<p class="page-context-note-copy">{{ note_copy|safe }}</p>',
    ),
    (
        "components/settings_shell.html",
        '<div class="section-header {{ class_name }}" {{ attrs|safe }}>',
    ),
    (
        "components/settings_shell.html",
        '<p class="section-description">{{ description|safe }}</p>',
    ),
    (
        "components/settings_shell.html",
        '<div class="section-card {{ class_name }}" {{ attrs|safe }}>',
    ),
    (
        "components/settings_shell.html",
        '<div class="settings-row {{ row_class }}" {{ attrs|safe }}>',
    ),
    (
        "components/settings_shell.html",
        '<p class="settings-row-help">{{ help|safe }}</p>',
    ),
    (
        "components/settings_shell.html",
        "<div class=\"settings-footer {{ 'settings-footer-end' if align == "
        "'end' else '' }} {{ class_name }}\" {{ attrs|safe }}>",
    ),
    (
        "components/settings_shell.html",
        '<div class="alert-banner alert-banner-{{ tone }} {{ class_name }}" {{ attrs|safe }}>',
    ),
    (
        "components/settings_shell.html",
        '<p class="alert-banner-description">{{ description|safe }}</p>',
    ),
    (
        "components/settings_shell.html",
        '<div class="inline-alert inline-alert-{{ tone }} {{ class_name }}" {{ attrs|safe }}>',
    ),
    (
        "components/settings_shell.html",
        '<p class="field-note field-note-{{ tone }} {{ class_name }}" {{ attrs|safe }}>',
    ),
    (
        "partials/import_history_results.html",
        "{{ job.created_at|localtime|safe if job.created_at else '—' }}",
    ),
}


def _template(path: str) -> str:
    return (TEMPLATE_ROOT / path).read_text(encoding="utf-8")


def test_template_safe_filter_usage_stays_reviewed() -> None:
    found: set[tuple[str, str]] = set()

    for template_path in TEMPLATE_ROOT.rglob("*.html"):
        relative_path = template_path.relative_to(TEMPLATE_ROOT).as_posix()
        for line in template_path.read_text(encoding="utf-8").splitlines():
            if "|safe" in line or "| safe" in line:
                found.add((relative_path, line.strip()))

    assert found == SAFE_FILTER_ALLOWLIST


def test_indexer_source_priority_uses_json_context_escape() -> None:
    html = _template("partials/settings_indexers.html")

    assert 'const stored = {{ configs.get("source_priority", sp_default) | tojson }};' in html
    assert 'configs.get("source_priority", sp_default) | safe' not in html


def test_naming_preview_escapes_inline_js_and_api_results() -> None:
    html = _template("partials/settings_naming.html")

    assert "previewNaming('{{" not in html
    assert "configs.get(" not in html
    assert "innerHTML" not in html
    assert 'x-text="example.input"' in html
    assert 'x-text="example.output"' in html
