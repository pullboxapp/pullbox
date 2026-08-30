"""Safe, data-only naming for optional story-arc placements."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from pullbox.core.naming import sanitize_for_filesystem

DEFAULT_STORY_ARC_FOLDER_TEMPLATE = "{StoryArc}"
DEFAULT_STORY_ARC_FILE_TEMPLATE = "{ReadingOrder:03d} - {Series} {IssueNumber}{IssueTitleOptional}"
# Explicit opt-in templates leave existing saved/default metadata policies unchanged.
ORIGINAL_STORY_ARC_FILE_TEMPLATE = "{OriginalFilename}"
ORDERED_ORIGINAL_STORY_ARC_FILE_TEMPLATE = "{ReadingOrder:02d} - {OriginalFilename}"

_MAX_TEMPLATE_BYTES = 1024
_TOKEN_RE = re.compile(r"\{(?P<name>[A-Za-z][A-Za-z0-9]*)(?::(?P<format>[^{}]+))?\}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_FOLDER_TOKENS = frozenset({"StoryArc", "Publisher", "StartYear", "EndYear", "SpanYears"})
_FILE_TOKENS = frozenset(
    {
        "ReadingOrder",
        "Series",
        "IssueNumber",
        "IssueTitle",
        "IssueTitleOptional",
        "Year",
        "Extension",
        "OriginalFilename",
    }
)
_READING_ORDER_FORMAT_RE = re.compile(r"0?[2-6]d")
_WINDOWS_DEVICE_NAME_RE = re.compile(
    r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9¹²³])", re.IGNORECASE
)


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
    publisher: str | None = None
    original_filename: str | None = None


class StoryArcOriginalFilenameError(ValueError):
    """A canonical basename cannot be preserved safely at the destination."""


def validate_story_arc_folder_template(template: str) -> None:
    """Reject path-producing or executable story-arc folder templates."""
    _validate_template(template, allowed_tokens=_FOLDER_TOKENS, kind="folder")


def validate_story_arc_file_template(template: str) -> None:
    """Reject path-producing or unbounded story-arc file templates."""
    tokens = _validate_template(template, allowed_tokens=_FILE_TOKENS, kind="file")
    names = [name for name, _format_spec in tokens]
    if "OriginalFilename" in names and (
        names.count("OriginalFilename") != 1
        or "Extension" in names
        or not template.endswith("{OriginalFilename}")
    ):
        msg = "OriginalFilename must appear once at the end, without an Extension token"
        raise ValueError(msg)
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
        "Publisher": safe(values.publisher) if values.publisher else "",
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
    safe_folder = safe(folder)
    if "{OriginalFilename}" in file_template:
        original = _validated_original_filename(values.original_filename, extension)
        prefix = _render_template(
            file_template.removesuffix("{OriginalFilename}"), file_values, values.reading_order
        )
        # Sanitize template-produced prefix text, not the preserved basename.
        # The sentinel retains an intentional trailing space in e.g. "01 - ".
        safe_prefix = safe(prefix + "x")[:-1]
        safe_file = _validated_original_filename(safe_prefix + original, extension)
    else:
        rendered_file = _render_template(file_template, file_values, values.reading_order)
        if "{Extension}" not in file_template:
            rendered_file = f"{rendered_file}.{extension}"
        safe_file = safe(rendered_file)
    if safe_folder in {"", ".", ".."} or safe_file in {"", ".", ".."}:
        msg = "Story-arc template rendered an unsafe path"
        raise ValueError(msg)
    return Path(safe_folder, safe_file)


def _validated_original_filename(filename: str | None, extension: str) -> str:
    """Preserve safe spelling, spacing and extension case; never silently rename."""
    if not filename:
        raise StoryArcOriginalFilenameError("Original filename requires a canonical file")
    try:
        encoded = filename.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise StoryArcOriginalFilenameError("Original filename is not valid UTF-8") from exc
    normalized_spaces = " ".join(filename.split())
    if (
        len(encoded) > 240
        or PureWindowsPath(filename).name != filename
        or re.search(r"[\x00-\x1f\x7f-\x9f]", filename)
        or filename.endswith((".", " "))
        or _WINDOWS_DEVICE_NAME_RE.fullmatch(filename.split(".", 1)[0].rstrip(" "))
        or sanitize_for_filesystem(normalized_spaces) != normalized_spaces
        or Path(filename).suffix.casefold() != f".{extension}"
    ):
        raise StoryArcOriginalFilenameError(
            "Original filename cannot be preserved safely with the canonical extension"
        )
    return filename


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
