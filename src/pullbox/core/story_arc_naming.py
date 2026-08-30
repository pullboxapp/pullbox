"""Safe, data-only naming for optional story-arc placements."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pullbox.core.naming import sanitize_for_filesystem

DEFAULT_STORY_ARC_FOLDER_TEMPLATE = "{StoryArc}"
DEFAULT_STORY_ARC_FILE_TEMPLATE = "{ReadingOrder:03d} - {Series} {IssueNumber}{IssueTitleOptional}"

_MAX_TEMPLATE_BYTES = 1024
_TOKEN_RE = re.compile(r"\{(?P<name>[A-Za-z][A-Za-z0-9]*)(?::(?P<format>[^{}]+))?\}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_FOLDER_TOKENS = frozenset({"StoryArc", "StartYear", "EndYear", "SpanYears"})
_FILE_TOKENS = frozenset(
    {
        "ReadingOrder",
        "Series",
        "IssueNumber",
        "IssueTitle",
        "IssueTitleOptional",
        "Year",
        "Extension",
    }
)
_READING_ORDER_FORMAT_RE = re.compile(r"0?[2-6]d")


@dataclass(frozen=True, slots=True)
class StoryArcNamingValues:
    """Bounded values used to preview or render one arc placement path."""

    story_arc: str
    reading_order: int
    series: str
    issue_number: str
    extension: str
    issue_title: str | None = None
    year: int | None = None
    start_year: int | None = None
    end_year: int | None = None


def validate_story_arc_folder_template(template: str) -> None:
    """Reject path-producing or executable story-arc folder templates."""
    _validate_template(template, allowed_tokens=_FOLDER_TOKENS, kind="folder")


def validate_story_arc_file_template(template: str) -> None:
    """Reject path-producing or unbounded story-arc file templates."""
    tokens = _validate_template(template, allowed_tokens=_FILE_TOKENS, kind="file")
    for name, format_spec in tokens:
        if name == "ReadingOrder":
            if format_spec is not None and not _READING_ORDER_FORMAT_RE.fullmatch(format_spec):
                msg = "ReadingOrder format must be a bounded width from 2d through 6d"
                raise ValueError(msg)
        elif format_spec is not None:
            msg = f"Formatting is not supported for story-arc token: {name}"
            raise ValueError(msg)


def render_story_arc_relative_path(
    values: StoryArcNamingValues,
    *,
    folder_template: str = DEFAULT_STORY_ARC_FOLDER_TEMPLATE,
    file_template: str = DEFAULT_STORY_ARC_FILE_TEMPLATE,
    replace_illegal: bool = True,
    colon_replacement: str = "dash",
) -> Path:
    """Render a sanitized relative folder/file path without touching disk."""
    validate_story_arc_folder_template(folder_template)
    validate_story_arc_file_template(file_template)
    if values.reading_order < 0:
        msg = "Story-arc reading order must not be negative"
        raise ValueError(msg)

    issue_number = values.issue_number.strip()
    if not issue_number or _CONTROL_RE.search(issue_number):
        msg = "Story-arc issue number must be a non-empty exact value"
        raise ValueError(msg)

    extension = values.extension.strip().lstrip(".").lower()
    if not re.fullmatch(r"[a-z0-9]{1,10}", extension):
        msg = "Story-arc source extension is invalid"
        raise ValueError(msg)

    def safe(value: str) -> str:
        return sanitize_for_filesystem(
            value,
            replace_illegal=replace_illegal,
            colon_replacement=colon_replacement,
        )

    start_year = str(values.start_year) if values.start_year is not None else ""
    end_year = str(values.end_year) if values.end_year is not None else ""
    if start_year and end_year and start_year != end_year:
        span_years = f"{start_year} - {end_year}"
    else:
        span_years = start_year or end_year

    folder_values = {
        "StoryArc": safe(values.story_arc),
        "StartYear": start_year,
        "EndYear": end_year,
        "SpanYears": span_years,
    }
    file_values = {
        "Series": safe(values.series),
        "IssueNumber": safe(issue_number),
        "IssueTitle": safe(values.issue_title) if values.issue_title else "",
        "IssueTitleOptional": f" - {safe(values.issue_title)}" if values.issue_title else "",
        "Year": str(values.year) if values.year is not None else "",
        "Extension": extension,
    }

    folder = _render_template(folder_template, folder_values, values.reading_order)
    rendered_file = _render_template(file_template, file_values, values.reading_order)
    if "{Extension}" not in file_template:
        rendered_file = f"{rendered_file}.{extension}"

    safe_folder = safe(folder)
    safe_file = safe(rendered_file)
    if safe_folder in {"", ".", ".."} or safe_file in {"", ".", ".."}:
        msg = "Story-arc template rendered an unsafe path"
        raise ValueError(msg)
    return Path(safe_folder, safe_file)


def _validate_template(
    template: str,
    *,
    allowed_tokens: frozenset[str],
    kind: str,
) -> tuple[tuple[str, str | None], ...]:
    if not template or len(template.encode("utf-8")) > _MAX_TEMPLATE_BYTES:
        msg = f"Story-arc {kind} template is empty or too long"
        raise ValueError(msg)
    if "/" in template or "\\" in template or _CONTROL_RE.search(template):
        msg = f"Story-arc {kind} template must produce one safe path segment"
        raise ValueError(msg)

    tokens: list[tuple[str, str | None]] = []
    for match in _TOKEN_RE.finditer(template):
        name = match.group("name")
        if name not in allowed_tokens:
            msg = f"Unsupported story-arc {kind} token: {name}"
            raise ValueError(msg)
        tokens.append((name, match.group("format")))

    without_tokens = _TOKEN_RE.sub("", template)
    if "{" in without_tokens or "}" in without_tokens:
        msg = f"Story-arc {kind} template contains an invalid token"
        raise ValueError(msg)
    return tuple(tokens)


def _render_template(
    template: str,
    values: dict[str, str],
    reading_order: int,
) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        if name == "ReadingOrder":
            format_spec = match.group("format")
            return format(reading_order, format_spec) if format_spec else str(reading_order)
        return values[name]

    return _TOKEN_RE.sub(replace, template)
