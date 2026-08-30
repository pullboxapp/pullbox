"""Versioned, data-only source layout templates for collection imports.

The grammar intentionally accepts only registered semantic tokens. User input
is escaped before the matcher is compiled; it never becomes executable regex,
code, shell syntax, or an absolute filesystem path.
"""

from __future__ import annotations

import enum
import re
import unicodedata
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath

from pullbox.core.source_metadata import SourceMetadataExtractor

LAYOUT_SCHEMA_VERSION = 1
MAX_LAYOUT_TEMPLATE_BYTES = 1024
MAX_LAYOUT_SEGMENT_BYTES = 255
MAX_LAYOUT_PATH_BYTES = 4096

_TOKEN_RE = re.compile(r"\{(?P<name>[A-Za-z][A-Za-z0-9]*)(?::(?P<format>[^{}]+))?\}")
_ISSUE_RE = re.compile(r"(?P<number>[+-]?\d+(?:\.\d+)?)(?P<suffix>[A-Za-z]*)")
_SERIES_YEAR_RE = re.compile(
    r"^(?P<series>.+?)\s*(?:[\[(](?P<bracketed_year>(?:19|20)\d{2})[\])]|"
    r"(?P<bare_year>(?:19|20)\d{2}))\s*$"
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_RESERVED_WINDOWS_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
)

_PATH_TOKENS = frozenset({"Publisher", "Series", "Year", "ComicVineId", "Type"})
_ISSUE_TOKENS = frozenset({"Publisher", "Series", "Year", "Issue", "IssueTitle", "Title", "Type"})
_FREE_TEXT_TOKENS = frozenset({"Publisher", "Series", "IssueTitle", "Type"})


class ImportLayoutMode(enum.StrEnum):
    """How Pullbox chooses a source layout matcher."""

    AUTO = "auto"
    PRESET = "preset"
    CUSTOM = "custom"


class LayoutClassification(enum.StrEnum):
    """Request-local classification of a source or layout cluster."""

    NORMAL_LIBRARY = "normal_library"
    STORY_ARC = "story_arc"
    MIXED = "mixed"
    NEEDS_REVIEW = "needs_review"


class LayoutTemplateError(ValueError):
    """A source-layout specification is unsupported, unsafe, or ambiguous."""


class LayoutValueError(ValueError):
    """A matched semantic value is unsafe to expose as layout evidence."""


@dataclass(frozen=True, slots=True)
class SourceLayoutSpec:
    """Serializable source-layout selection stored by a later import snapshot."""

    schema_version: int = LAYOUT_SCHEMA_VERSION
    mode: ImportLayoutMode = ImportLayoutMode.AUTO
    preset: str | None = None
    series_path_template: str | None = None
    issue_filename_template: str | None = None
    selected_cluster_id: str | None = None
    fallback_to_auto: bool = True

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic JSON-compatible representation."""
        return {
            "schema_version": self.schema_version,
            "mode": self.mode.value,
            "preset": self.preset,
            "series_path_template": self.series_path_template,
            "issue_filename_template": self.issue_filename_template,
            "selected_cluster_id": self.selected_cluster_id,
            "fallback_to_auto": self.fallback_to_auto,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> SourceLayoutSpec:
        """Restore a layout spec from its JSON-compatible representation."""
        try:
            mode = ImportLayoutMode(str(value.get("mode", ImportLayoutMode.AUTO.value)))
        except ValueError as exc:
            raise LayoutTemplateError("Unknown source layout mode") from exc
        return cls(
            schema_version=_required_int(
                value.get("schema_version", LAYOUT_SCHEMA_VERSION),
                field_name="schema_version",
            ),
            mode=mode,
            preset=_optional_string(value.get("preset")),
            series_path_template=_optional_string(value.get("series_path_template")),
            issue_filename_template=_optional_string(value.get("issue_filename_template")),
            selected_cluster_id=_optional_string(value.get("selected_cluster_id")),
            fallback_to_auto=_required_bool(
                value.get("fallback_to_auto", True),
                field_name="fallback_to_auto",
            ),
        )


@dataclass(frozen=True, slots=True)
class SourceLayoutMatch:
    """Semantic evidence decoded from one root-relative comic path."""

    relative_path: str
    publisher: str | None = None
    series: str | None = None
    year: int | None = None
    issue_number: str | None = None
    issue_title: str | None = None
    comicvine_id: int | None = None
    issue_type: str | None = None


@dataclass(frozen=True, slots=True)
class _TemplateToken:
    semantic_name: str
    capture_name: str
    value_pattern: str
    numeric_width: int | None = None


@dataclass(frozen=True, slots=True)
class _CompiledSegment:
    pattern: re.Pattern[str]
    pattern_text: str
    tokens: tuple[_TemplateToken, ...]

    def match(
        self,
        value: str,
        existing_captures: dict[str, list[str]] | None = None,
    ) -> dict[str, list[str]] | None:
        pattern = self.pattern
        if existing_captures:
            constrained_pattern = self.pattern_text
            constrained = False
            for token in self.tokens:
                existing_values = existing_captures.get(token.semantic_name)
                if token.semantic_name not in _FREE_TEXT_TOKENS or not existing_values:
                    continue
                original = f"(?P<{token.capture_name}>{token.value_pattern})"
                replacement = (
                    f"(?P<{token.capture_name}>{_escaped_literal_pattern(existing_values[0])})"
                )
                constrained_pattern = constrained_pattern.replace(original, replacement, 1)
                constrained = True
            if constrained:
                pattern = re.compile(constrained_pattern, flags=re.IGNORECASE)

        matched = pattern.fullmatch(value)
        if matched is None:
            return None
        captures: dict[str, list[str]] = {}
        for token in self.tokens:
            raw_value = matched.group(token.capture_name)
            normalized = _normalize_token_value(token.semantic_name, raw_value)
            captures.setdefault(token.semantic_name, []).append(normalized)
        return captures


@dataclass(frozen=True, slots=True)
class CompiledSourceLayout:
    """A bounded matcher compiled from a validated source-layout spec."""

    spec: SourceLayoutSpec
    path_segments: tuple[_CompiledSegment, ...]
    filename_segment: _CompiledSegment | None

    def match(self, relative_path: str | PurePosixPath) -> SourceLayoutMatch | None:
        """Match one comic path relative to the selected import root."""
        raw_path = str(relative_path)
        _validate_relative_input_path(raw_path)
        path = PurePosixPath(raw_path)
        if len(path.parts) < 2 or len(path.parts[:-1]) != len(self.path_segments):
            return None

        captures: dict[str, list[str]] = {}
        for compiled, value in zip(self.path_segments, path.parts[:-1], strict=True):
            segment_captures = compiled.match(value, captures)
            if segment_captures is None:
                return None
            _extend_captures(captures, segment_captures)

        if self.filename_segment is not None:
            filename_captures = self.filename_segment.match(path.stem, captures)
            if filename_captures is None:
                return None
            _extend_captures(captures, filename_captures)
        else:
            metadata = SourceMetadataExtractor().from_release_title(
                path.name,
                folder_name=path.parent.name,
            )
            if metadata.issue_number is not None:
                captures.setdefault("Issue", []).append(
                    _normalize_issue_value(str(metadata.issue_number))
                )
            if metadata.year is not None:
                captures.setdefault("Year", []).append(str(metadata.year))

        agreed = _agreed_capture_values(captures)
        if agreed is None:
            return None
        series = agreed.get("Series")
        year = _optional_int(agreed.get("Year"))
        if series is not None and year is None:
            series, inferred_year = split_series_year(series)
            year = inferred_year

        return SourceLayoutMatch(
            relative_path=path.as_posix(),
            publisher=agreed.get("Publisher"),
            series=series,
            year=year,
            issue_number=agreed.get("Issue"),
            issue_title=agreed.get("IssueTitle"),
            comicvine_id=_optional_int(agreed.get("ComicVineId")),
            issue_type=agreed.get("Type"),
        )


_PRESETS: dict[str, SourceLayoutSpec] = {
    "series_folders": SourceLayoutSpec(
        mode=ImportLayoutMode.PRESET,
        preset="series_folders",
        series_path_template="{Series}",
    ),
    "publisher_series": SourceLayoutSpec(
        mode=ImportLayoutMode.PRESET,
        preset="publisher_series",
        series_path_template="{Publisher}/{Series}",
    ),
}


def resolve_source_layout_spec(spec: SourceLayoutSpec) -> SourceLayoutSpec:
    """Validate and normalize a registered or custom source layout."""
    if spec.schema_version != LAYOUT_SCHEMA_VERSION:
        raise LayoutTemplateError(
            f"Unsupported source layout schema version: {spec.schema_version}"
        )
    if spec.selected_cluster_id is not None and (
        not spec.selected_cluster_id
        or len(spec.selected_cluster_id.encode("utf-8")) > 128
        or _CONTROL_RE.search(spec.selected_cluster_id)
    ):
        raise LayoutTemplateError("Selected layout cluster ID is invalid")

    if spec.mode == ImportLayoutMode.AUTO:
        if spec.preset or spec.series_path_template or spec.issue_filename_template:
            raise LayoutTemplateError("Automatic layout cannot include preset or custom templates")
        return spec

    if spec.mode == ImportLayoutMode.PRESET:
        if not spec.preset or spec.preset not in _PRESETS:
            raise LayoutTemplateError("Unknown source layout preset")
        registered = _PRESETS[spec.preset]
        if spec.series_path_template not in {None, registered.series_path_template}:
            raise LayoutTemplateError("Preset path template does not match the registry")
        if spec.issue_filename_template not in {None, registered.issue_filename_template}:
            raise LayoutTemplateError("Preset filename template does not match the registry")
        return replace(
            spec,
            series_path_template=registered.series_path_template,
            issue_filename_template=registered.issue_filename_template,
        )

    if spec.mode != ImportLayoutMode.CUSTOM:
        raise LayoutTemplateError("Unknown source layout mode")
    if spec.preset is not None:
        raise LayoutTemplateError("Custom layout cannot include a preset")
    if not spec.series_path_template:
        raise LayoutTemplateError("Custom layout requires a series path template")
    return spec


def compile_source_layout(spec: SourceLayoutSpec) -> CompiledSourceLayout:
    """Compile a preset or custom spec into escaped, deterministic matchers."""
    effective = resolve_source_layout_spec(spec)
    if effective.mode == ImportLayoutMode.AUTO or effective.series_path_template is None:
        raise LayoutTemplateError(
            "Automatic layout is resolved per path and cannot be compiled once"
        )

    path_segments = _compile_path_template(effective.series_path_template)
    filename_segment = (
        _compile_filename_template(effective.issue_filename_template)
        if effective.issue_filename_template is not None
        else None
    )
    return CompiledSourceLayout(
        spec=effective,
        path_segments=path_segments,
        filename_segment=filename_segment,
    )


def split_series_year(value: str) -> tuple[str, int | None]:
    """Split supported inline or bracketed year suffixes from a series segment."""
    matched = _SERIES_YEAR_RE.fullmatch(value.strip())
    if matched is None:
        return value.strip(), None
    year_text = matched.group("bracketed_year") or matched.group("bare_year")
    return matched.group("series").strip(), int(year_text)


def registered_source_layout_presets() -> tuple[SourceLayoutSpec, ...]:
    """Return registered presets in stable identifier order."""
    return tuple(_PRESETS[key] for key in sorted(_PRESETS))


def _compile_path_template(template: str) -> tuple[_CompiledSegment, ...]:
    _validate_template_size(template)
    if template.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", template):
        raise LayoutTemplateError("Source layout paths must be root-relative")
    if "\\" in template:
        raise LayoutTemplateError("Source layout paths must use forward-slash separators")
    parts = template.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise LayoutTemplateError("Source layout contains an empty or unsafe path segment")
    return tuple(_compile_segment(part, allowed_tokens=_PATH_TOKENS) for part in parts)


def _compile_filename_template(template: str) -> _CompiledSegment:
    if re.search(r"\.(?:cbz|cbr|cb7|cbt|pdf|epub)$", template, flags=re.IGNORECASE):
        raise LayoutTemplateError("Comic extensions are handled separately from layout tokens")
    return _compile_segment(template, allowed_tokens=_ISSUE_TOKENS)


def _compile_segment(template: str, *, allowed_tokens: frozenset[str]) -> _CompiledSegment:
    _validate_template_size(template)
    if len(template.encode("utf-8")) > MAX_LAYOUT_SEGMENT_BYTES:
        raise LayoutTemplateError("A layout segment exceeds the supported byte length")
    if "/" in template or "\\" in template:
        raise LayoutTemplateError("A layout segment cannot contain a path separator")
    if _CONTROL_RE.search(template):
        raise LayoutTemplateError("A layout template cannot contain control characters")

    pattern_parts: list[str] = []
    tokens: list[_TemplateToken] = []
    cursor = 0
    previous_was_token = False
    for index, matched in enumerate(_TOKEN_RE.finditer(template)):
        literal = template[cursor : matched.start()]
        if "{" in literal or "}" in literal:
            raise LayoutTemplateError("A layout template contains an invalid token")
        if previous_was_token and not literal:
            raise LayoutTemplateError("Adjacent semantic tokens are ambiguous")
        pattern_parts.append(_escaped_literal_pattern(literal))

        raw_name = matched.group("name")
        semantic_name = "IssueTitle" if raw_name == "Title" else raw_name
        if raw_name not in allowed_tokens:
            raise LayoutTemplateError(f"Unknown or unsupported layout token: {raw_name}")
        raw_format = matched.group("format")
        numeric_width = _validate_token_format(semantic_name, raw_format)
        capture_name = f"layout_value_{index}"
        value_pattern = _token_value_pattern(semantic_name)
        pattern_parts.append(f"(?P<{capture_name}>{value_pattern})")
        tokens.append(
            _TemplateToken(
                semantic_name=semantic_name,
                capture_name=capture_name,
                value_pattern=value_pattern,
                numeric_width=numeric_width,
            )
        )
        cursor = matched.end()
        previous_was_token = True

    trailing = template[cursor:]
    if "{" in trailing or "}" in trailing:
        raise LayoutTemplateError("A layout template contains an invalid token")
    pattern_parts.append(_escaped_literal_pattern(trailing))
    if not tokens:
        raise LayoutTemplateError("A layout template must include at least one semantic token")

    pattern_text = "".join(pattern_parts)
    return _CompiledSegment(
        pattern=re.compile(pattern_text, flags=re.IGNORECASE),
        pattern_text=pattern_text,
        tokens=tuple(tokens),
    )


def _validate_template_size(template: str) -> None:
    if not template or len(template.encode("utf-8")) > MAX_LAYOUT_TEMPLATE_BYTES:
        raise LayoutTemplateError("A layout template is empty or too long")


def _validate_token_format(semantic_name: str, raw_format: str | None) -> int | None:
    if raw_format is None:
        return None
    if semantic_name != "Issue":
        raise LayoutTemplateError(f"Formatting is not supported for {semantic_name}")
    matched = re.fullmatch(r"0?(?P<width>[1-9])d", raw_format)
    if matched is None:
        raise LayoutTemplateError("Issue format must be a decimal width such as 02d or 03d")
    return int(matched.group("width"))


def _token_value_pattern(semantic_name: str) -> str:
    if semantic_name == "Issue":
        value_pattern = r"[+-]?\d+(?:\.\d+)?[A-Za-z]*"
    elif semantic_name == "Year":
        value_pattern = r"(?:19|20)\d{2}"
    elif semantic_name == "ComicVineId":
        value_pattern = r"\d+"
    elif semantic_name in _FREE_TEXT_TOKENS:
        value_pattern = r".+?"
    else:
        raise LayoutTemplateError(f"Unsupported semantic token: {semantic_name}")
    return value_pattern


def _escaped_literal_pattern(value: str) -> str:
    parts: list[str] = []
    cursor = 0
    for matched in re.finditer(r"\s+", value):
        parts.append(re.escape(value[cursor : matched.start()]))
        parts.append(r"\s+")
        cursor = matched.end()
    parts.append(re.escape(value[cursor:]))
    return "".join(parts)


def _normalize_token_value(semantic_name: str, raw_value: str) -> str:
    value = unicodedata.normalize("NFC", raw_value.strip())
    if not value:
        raise LayoutValueError(f"{semantic_name} cannot be empty")
    if len(value.encode("utf-8")) > MAX_LAYOUT_SEGMENT_BYTES:
        raise LayoutValueError(f"{semantic_name} exceeds the supported byte length")
    if _CONTROL_RE.search(value) or "/" in value or "\\" in value:
        raise LayoutValueError(f"{semantic_name} contains an unsafe character")
    reserved_base = value.rstrip(" .").split(".", 1)[0].upper()
    if reserved_base in _RESERVED_WINDOWS_NAMES:
        raise LayoutValueError(f"{semantic_name} is a reserved filesystem name")
    if semantic_name == "Issue":
        return _normalize_issue_value(value)
    if semantic_name in {"Year", "ComicVineId"}:
        return str(int(value))
    return re.sub(r"\s+", " ", value)


def _normalize_issue_value(value: str) -> str:
    matched = _ISSUE_RE.fullmatch(value.strip())
    if matched is None:
        raise LayoutValueError("Issue is not a supported number")
    try:
        number = Decimal(matched.group("number"))
    except InvalidOperation as exc:
        raise LayoutValueError("Issue is not a supported number") from exc
    normalized = format(number, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    if normalized == "-0":
        normalized = "0"
    return f"{normalized}{matched.group('suffix')}"


def _agreed_capture_values(captures: dict[str, list[str]]) -> dict[str, str] | None:
    agreed: dict[str, str] = {}
    for semantic_name, values in captures.items():
        first = values[0]
        comparison = _comparison_value(semantic_name, first)
        if any(_comparison_value(semantic_name, value) != comparison for value in values[1:]):
            return None
        agreed[semantic_name] = first
    return agreed


def _comparison_value(semantic_name: str, value: str) -> str:
    if semantic_name in {"Issue", "Year", "ComicVineId"}:
        return value.casefold()
    return re.sub(r"\s+", " ", value).casefold()


def _extend_captures(
    target: dict[str, list[str]],
    source: dict[str, list[str]],
) -> None:
    for key, values in source.items():
        target.setdefault(key, []).extend(values)


def _validate_relative_input_path(raw_path: str) -> None:
    if not raw_path or len(raw_path.encode("utf-8")) > MAX_LAYOUT_PATH_BYTES:
        raise LayoutValueError("Relative source path is empty or too long")
    if raw_path.startswith(("/", "\\")) or "\\" in raw_path:
        raise LayoutValueError("Source path must be root-relative and use forward slashes")
    if _CONTROL_RE.search(raw_path):
        raise LayoutValueError("Source path contains a control character")
    raw_parts = raw_path.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise LayoutValueError("Source path contains an unsafe segment")


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: str | None) -> int | None:
    return int(value) if value is not None else None


def _required_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise LayoutTemplateError(f"{field_name} must be an integer")
    try:
        return int(value)
    except ValueError as exc:
        raise LayoutTemplateError(f"{field_name} must be an integer") from exc


def _required_bool(value: object, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise LayoutTemplateError(f"{field_name} must be a boolean")
    return value
