"""Filename parsing, formatting, and series folder naming for comic book files.

Handles common naming conventions with priority-ordered regex patterns.
Provides folder naming, per-type file naming, filesystem sanitization,
type detection, and preview examples for the settings UI.

Pure functions suitable for direct testing without I/O dependencies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pullbox.core.naming_type_detection import (
    classify_series_type as classify_series_type,
)
from pullbox.core.naming_type_detection import (
    detect_issue_type as detect_issue_type,
)
from pullbox.core.naming_type_detection import (
    detect_issue_type_from_metadata_title as detect_issue_type_from_metadata_title,
)
from pullbox.core.naming_type_detection import (
    detect_series_type as detect_series_type,
)
from pullbox.core.naming_type_detection import (
    detect_series_type_from_description as detect_series_type_from_description,
)
from pullbox.core.naming_type_detection import (
    detect_series_type_from_issue_count as detect_series_type_from_issue_count,
)
from pullbox.core.naming_type_detection import (
    extract_base_series_title as extract_base_series_title,
)

# ---------------------------------------------------------------------------
# Parsing patterns — priority ordered, first match wins
# ---------------------------------------------------------------------------

_PATTERNS = [
    # "Batman (2016) #045 (Digital) (Zone-Empire).cbz"
    re.compile(r"^(?P<series>.+?)\s*\((?P<year>\d{4})\)\s*#(?P<issue>[\d.]+)"),
    # "Batman 045 (2016) (Digital-Empire).cbz"
    re.compile(r"^(?P<series>.+?)\s+(?P<issue>[\d.]+)\s*\((?P<year>\d{4})\)"),
    # "Batman #045 (2016).cbz"
    re.compile(r"^(?P<series>.+?)\s*#(?P<issue>[\d.]+)\s*\((?P<year>\d{4})\)"),
    # "Batman 045.cbz" (minimal)
    re.compile(r"^(?P<series>.+?)\s+(?P<issue>[\d.]+)\s*$"),
    # "Batman #45.cbz" (minimal with hash)
    re.compile(r"^(?P<series>.+?)\s*#(?P<issue>[\d.]+)"),
]

# Tags extracted from filenames (case-insensitive).
_TAG_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Digital", re.compile(r"\bDigital\b", re.IGNORECASE)),
    ("Webrip", re.compile(r"\bWebrip\b", re.IGNORECASE)),
    ("c2c", re.compile(r"\bc2c\b", re.IGNORECASE)),
    ("noads", re.compile(r"\bnoads\b", re.IGNORECASE)),
    ("HD", re.compile(r"\bHD\b")),
    ("Complete", re.compile(r"\bComplete\b", re.IGNORECASE)),
    ("Annual", re.compile(r"\bAnnual\b", re.IGNORECASE)),
    ("Special", re.compile(r"\bSpecial\b", re.IGNORECASE)),
    ("TPB", re.compile(r"\bTPB\b", re.IGNORECASE)),
    ("Omnibus", re.compile(r"\bOmnibus\b", re.IGNORECASE)),
]

# File extensions to strip before parsing.
_COMIC_EXTENSIONS = re.compile(r"\.(cbz|cbr|cb7|pdf)$", re.IGNORECASE)

# Characters illegal in file/folder names across Windows, macOS, Linux.
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Windows reserved device names (cannot be used as folder/file names).
_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

# Colon replacement strategies.
_COLON_REPLACEMENTS = {
    "dash": " -",
    "space": " ",
    "empty": "",
    "smart": " \u2014",  # em-dash
}


def _collapse_whitespace(value: str) -> str:
    return " ".join(value.split())


def _replace_slash_separators(value: str) -> str:
    """Replace slash-like title separators without regex backtracking."""
    output: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char in {"/", "\\"}:
            while output and output[-1].isspace():
                output.pop()
            if output and output[-1] != " ":
                output.append(" ")
            output.extend(("-", " "))
            index += 1
            while index < len(value) and value[index].isspace():
                index += 1
            continue
        output.append(char)
        index += 1
    return "".join(output)


def _strip_trailing_separators(value: str) -> str:
    return value.rstrip(" \t\r\n\f\v-")


_NAMING_TYPE_DISPLAY_NAMES: dict[str, str] = {
    "issue": "",
    "standard": "",
    "annual": "Annual",
    "tpb": "TPB",
    "volume": "Vol",
    "omnibus": "Omnibus",
    "compendium": "Compendium",
    "gn": "GN",
    "ogn": "OGN",
    "graphic_novel": "GN",
    "hc": "HC",
    "hardcover": "HC",
    "one_shot": "One-Shot",
    "special": "Special",
    "deluxe": "Deluxe",
}

_COLLECTION_STYLE_ISSUE_TYPES: frozenset[str] = frozenset(
    {"tpb", "volume", "omnibus", "compendium", "hc", "deluxe"}
)
_SINGLE_RELEASE_ISSUE_TYPES: frozenset[str] = frozenset(
    {"gn", "ogn", "graphic_novel", "one_shot", "special"}
)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedFilename:
    """Result of parsing a comic book filename."""

    series: str
    issue_number: float
    year: int | None = None
    volume: str | None = None
    issue_type: str = "issue"  # IssueType.value string
    tags: list[str] = field(default_factory=list)
    extension: str | None = None


@dataclass(frozen=True)
class NamingPreviewExample:
    """A single preview example for the naming settings UI."""

    series: str
    year: int | None = None
    issue: float | None = None
    volume: int | None = None
    issue_type: str = "issue"  # string value of IssueType enum
    title: str | None = None
    publisher: str | None = None
    comicvine_id: int | None = None


LEGACY_NON_STANDARD_FILE_TEMPLATE = "{Series} ({Year}) {Type}"
PREVIOUS_DEFAULT_NON_STANDARD_FILE_TEMPLATE = "{Series} ({Year}) {Type} - {Title}"
DEFAULT_COLLECTION_NON_STANDARD_FILE_TEMPLATE = "{Series} ({Year}) {Type} {Volume:02d} - {Title}"
DEFAULT_SINGLE_NON_STANDARD_FILE_TEMPLATE = "{Series} ({Year}) {Type} - {Title}"
DEFAULT_NON_STANDARD_FILE_TEMPLATE = DEFAULT_COLLECTION_NON_STANDARD_FILE_TEMPLATE


def resolve_collection_non_standard_file_template(template: str | None) -> str:
    """Return the effective collection-style non-standard template."""
    if template is None:
        return DEFAULT_COLLECTION_NON_STANDARD_FILE_TEMPLATE

    normalized = template.strip()
    if not normalized or normalized in {
        LEGACY_NON_STANDARD_FILE_TEMPLATE,
        PREVIOUS_DEFAULT_NON_STANDARD_FILE_TEMPLATE,
    }:
        return DEFAULT_COLLECTION_NON_STANDARD_FILE_TEMPLATE
    return normalized


def resolve_single_non_standard_file_template(template: str | None) -> str:
    """Return the effective single-release non-standard template."""
    if template is None:
        return DEFAULT_SINGLE_NON_STANDARD_FILE_TEMPLATE

    normalized = template.strip()
    if not normalized or normalized in {
        LEGACY_NON_STANDARD_FILE_TEMPLATE,
        PREVIOUS_DEFAULT_NON_STANDARD_FILE_TEMPLATE,
    }:
        return DEFAULT_SINGLE_NON_STANDARD_FILE_TEMPLATE
    return normalized


def resolve_non_standard_file_template(template: str | None) -> str:
    """Backward-compatible alias for the collection non-standard template."""
    return resolve_collection_non_standard_file_template(template)


# ---------------------------------------------------------------------------
# Preview examples — curated from NZBGeek test fixtures
# ---------------------------------------------------------------------------

PREVIEW_EXAMPLES: list[NamingPreviewExample] = [
    # Standard issues
    NamingPreviewExample(
        series="Absolute Batman",
        year=2024,
        issue=17,
        issue_type="issue",
        publisher="DC Comics",
        comicvine_id=162083,
    ),
    NamingPreviewExample(
        series="Amazing Spider-Man",
        year=2022,
        issue=22,
        issue_type="issue",
        publisher="Marvel",
        comicvine_id=140120,
    ),
    NamingPreviewExample(
        series="X-Men",
        year=2024,
        issue=25,
        issue_type="issue",
        publisher="Marvel",
        comicvine_id=155827,
    ),
    # Annuals
    NamingPreviewExample(
        series="Hellblazer",
        year=1988,
        issue=1,
        issue_type="annual",
        publisher="DC Comics",
        comicvine_id=4949,
    ),
    NamingPreviewExample(
        series="Poison Ivy",
        year=2022,
        issue=1,
        issue_type="annual",
        publisher="DC Comics",
        comicvine_id=164200,
    ),
    # TPB
    NamingPreviewExample(
        series="Werewolf By Night - The Howling Tome",
        year=2025,
        volume=1,
        issue_type="tpb",
        title="The Howling Tome",
        publisher="Marvel",
        comicvine_id=170100,
    ),
    NamingPreviewExample(
        series="East of West",
        year=2014,
        volume=3,
        issue_type="tpb",
        title="There Is No Us",
        publisher="Image Comics",
        comicvine_id=49563,
    ),
    # Omnibus
    NamingPreviewExample(
        series="Beasts of Burden",
        year=2025,
        volume=1,
        issue_type="omnibus",
        title="Animal Rites",
        publisher="Dark Horse",
        comicvine_id=25678,
    ),
    NamingPreviewExample(
        series="Darkwing Duck",
        year=2026,
        volume=1,
        issue_type="omnibus",
        title="Justice Ducks",
        publisher="Dynamite",
        comicvine_id=180234,
    ),
    # Volume
    NamingPreviewExample(
        series="AL15",
        year=2021,
        volume=2,
        issue_type="volume",
        title="Broken Dreams",
        publisher="AWA Studios",
        comicvine_id=136732,
    ),
    # One Shot
    NamingPreviewExample(
        series="Ignite Prime",
        year=2026,
        issue_type="one_shot",
        title="Zero Hour",
        publisher="Ignite Comics",
        comicvine_id=190001,
    ),
    # Graphic Novel
    NamingPreviewExample(
        series="The College Try",
        year=2026,
        issue_type="gn",
        title="Freshman Year",
        publisher="Mad Cave Studios",
        comicvine_id=191002,
    ),
    # Hardcover
    NamingPreviewExample(
        series="Cairo",
        year=2007,
        volume=1,
        issue_type="hc",
        title="City of Sand",
        publisher="DC Comics",
        comicvine_id=10234,
    ),
    # Special
    NamingPreviewExample(
        series="Silverline Christmas 25",
        year=2025,
        issue=1,
        issue_type="special",
        title="Holiday Special",
        publisher="Silverline Comics",
        comicvine_id=185001,
    ),
    # Deluxe Edition
    NamingPreviewExample(
        series="Batman - Year Three",
        year=2025,
        volume=1,
        issue_type="deluxe",
        title="Deluxe Edition",
        publisher="DC Comics",
        comicvine_id=186001,
    ),
    # Compendium
    NamingPreviewExample(
        series="Rising Stars",
        year=2008,
        volume=1,
        issue_type="compendium",
        title="The Definitive Collection",
        publisher="Image Comics",
        comicvine_id=12345,
    ),
]


# ---------------------------------------------------------------------------
# Filesystem sanitization
# ---------------------------------------------------------------------------


def sanitize_for_filesystem(
    name: str,
    *,
    replace_illegal: bool = True,
    colon_replacement: str = "dash",
) -> str:
    """Make a string safe for use as a file or folder name.

    Args:
        name: The raw name to sanitize.
        replace_illegal: Whether to strip illegal characters.
        colon_replacement: How to replace colons — one of
            ``"dash"`` (`` -``), ``"space"``, ``"empty"``, ``"smart"`` (em-dash).

    Returns:
        A filesystem-safe string, trimmed and with no trailing dots/spaces.
    """
    if not name:
        return "Unknown"

    if replace_illegal:
        # Handle colons specially before stripping other illegal chars
        colon_repl = _COLON_REPLACEMENTS.get(colon_replacement, " -")
        name = name.replace(":", colon_repl)

        # Slash-like separators are illegal on disk but meaningful in comic titles.
        name = _replace_slash_separators(name)

        # Remove remaining illegal characters
        name = _ILLEGAL_CHARS.sub("", name)

    # Collapse multiple spaces
    name = _collapse_whitespace(name)

    # Strip trailing dots and spaces (Windows restriction)
    name = name.rstrip(". ")

    # Handle reserved device names
    stem = name.split(".")[0].upper()
    if stem in _RESERVED_NAMES:
        name = f"_{name}"

    # Truncate to a reasonable filesystem limit (255 bytes is common)
    if len(name.encode("utf-8")) > 240:
        # Trim conservatively, leaving room for extensions
        while len(name.encode("utf-8")) > 230 and len(name) > 1:
            name = name[:-1]
        name = name.rstrip(". ")

    return name or "Unknown"


# ---------------------------------------------------------------------------
# Series folder formatting
# ---------------------------------------------------------------------------


def _series_type_display_name(series_type_value: str | None) -> str:
    """Get the canonical naming display for a series type value."""
    if not series_type_value:
        return ""
    normalized = str(series_type_value).strip().lower()
    if not normalized or normalized == "standard":
        return ""
    if normalized in _NAMING_TYPE_DISPLAY_NAMES:
        return _NAMING_TYPE_DISPLAY_NAMES[normalized]
    return normalized.replace("_", " ").title()


def format_series_folder(
    title: str,
    year: int | None = None,
    publisher: str | None = None,
    comicvine_id: int | None = None,
    series_type: str | None = None,
    *,
    template: str = "{Series} ({Year})",
    replace_illegal: bool = True,
    colon_replacement: str = "dash",
) -> str:
    """Format a series folder name from metadata and a template.

    Supported tokens: ``{Series}``, ``{Year}``, ``{Publisher}``,
    ``{ComicVineId}``, ``{Type}``

    Args:
        title: Series title.
        year: Start year.
        publisher: Publisher name.
        comicvine_id: ComicVine series ID.
        series_type: String value of ``SeriesType`` enum (e.g. ``"tpb"``).
        template: Folder naming template.
        replace_illegal: Sanitize illegal filesystem characters.
        colon_replacement: How to handle colons.

    Returns:
        Filesystem-safe folder name.
    """
    year_str = str(year) if year else "Unknown"
    publisher_str = publisher or "Unknown"
    cvid_str = str(comicvine_id) if comicvine_id else "Unknown"
    type_display = _series_type_display_name(series_type)

    # Sanitize individual components first
    clean_title = sanitize_for_filesystem(
        title,
        replace_illegal=replace_illegal,
        colon_replacement=colon_replacement,
    )
    clean_publisher = sanitize_for_filesystem(
        publisher_str,
        replace_illegal=replace_illegal,
        colon_replacement=colon_replacement,
    )

    name = template
    name = name.replace("{Series}", clean_title)
    name = name.replace("{Year}", year_str)
    name = name.replace("{Publisher}", clean_publisher)
    name = name.replace("{ComicVineId}", cvid_str)
    name = name.replace("{Type}", type_display)

    # Clean up empty parens/brackets from missing tokens (e.g. [{Type}] when standard)
    name = re.sub(r"\(\s*\)", "", name)
    name = re.sub(r"\[\s*\]", "", name)
    name = _collapse_whitespace(name)

    # Final sanitization pass on the assembled result
    return sanitize_for_filesystem(
        name,
        replace_illegal=replace_illegal,
        colon_replacement=colon_replacement,
    )


# ---------------------------------------------------------------------------
# Comic file formatting (enhanced)
# ---------------------------------------------------------------------------


def _issue_type_display_name(issue_type_value: str) -> str:
    """Get the canonical naming display for an issue type value."""
    normalized = str(issue_type_value).strip().lower()
    if not normalized or normalized == "issue":
        return ""
    if normalized in _NAMING_TYPE_DISPLAY_NAMES:
        return _NAMING_TYPE_DISPLAY_NAMES[normalized]
    return normalized.replace("_", " ").title()


def issue_type_supports_volume(issue_type_value: str | None) -> bool:
    """Return True when the naming contract should synthesize a {Volume} token."""
    if not issue_type_value:
        return False
    return str(issue_type_value).strip().lower() in _COLLECTION_STYLE_ISSUE_TYPES


def issue_type_uses_collection_template(issue_type_value: str | None) -> bool:
    """Return True when a non-standard issue should use the collection template."""
    return issue_type_supports_volume(issue_type_value)


def issue_type_uses_single_release_template(issue_type_value: str | None) -> bool:
    """Return True when a non-standard issue should use the single-release template."""
    if not issue_type_value:
        return False
    return str(issue_type_value).strip().lower() in _SINGLE_RELEASE_ISSUE_TYPES


def normalize_issue_type_for_naming(
    issue_type_value: str | None,
    *,
    collection_series_entry_count: int | None = None,
) -> str:
    """Normalize an issue type for filename rendering.

    Mixed numbered collection series should render consistently as ``Vol``
    rather than mixing ``TPB`` and ``Vol`` labels.
    """
    if not issue_type_value:
        return ""

    normalized = str(issue_type_value).strip().lower()
    if (
        collection_series_entry_count
        and collection_series_entry_count > 1
        and normalized in {"tpb", "volume"}
    ):
        return "volume"
    return normalized


def format_comic_file(
    series: str,
    year: int | None = None,
    issue: float | None = None,
    volume: int | None = None,
    issue_type: str = "issue",
    title: str | None = None,
    publisher: str | None = None,
    edition: str | None = None,
    *,
    template: str = "{Series} ({Year}) #{Issue:03d}",
    extension: str = "cbz",
    replace_illegal: bool = True,
    colon_replacement: str = "dash",
) -> str:
    """Format a comic filename from metadata and a template.

    Supported tokens:
        ``{Series}``, ``{Year}``, ``{Issue}``, ``{Issue:03d}``,
        ``{Volume}``, ``{Volume:02d}``, ``{Type}``, ``{IssueTitle}``,
        ``{Title}``, ``{Publisher}``

    Args:
        series: Series title.
        year: Year (from series or release date).
        issue: Issue number (may be ``None`` for one-shots/GNs).
        volume: Volume number (for TPBs, omnibuses, etc.).
        issue_type: String value of ``IssueType`` enum.
        title: Issue/collection title.
        publisher: Publisher name.
        template: Naming template.
        extension: File extension without dot.
        replace_illegal: Sanitize illegal filesystem characters.
        colon_replacement: How to handle colons.

    Returns:
        Formatted filename with extension.
    """
    year_str = str(year) if year else "Unknown"
    type_display = _issue_type_display_name(issue_type)
    title_str = (
        sanitize_for_filesystem(
            title,
            replace_illegal=replace_illegal,
            colon_replacement=colon_replacement,
        ).strip()
        if title
        else ""
    )
    publisher_str = (
        sanitize_for_filesystem(
            publisher or "Unknown",
            replace_illegal=replace_illegal,
            colon_replacement=colon_replacement,
        ).strip()
        if publisher
        else "Unknown"
    )
    _ = edition

    # Sanitize series
    clean_series = sanitize_for_filesystem(
        series,
        replace_illegal=replace_illegal,
        colon_replacement=colon_replacement,
    )

    name = template
    name = name.replace("{Series}", clean_series)
    name = name.replace("{Year}", year_str)
    name = name.replace("{Type}", type_display)
    name = name.replace("{IssueTitle}", title_str)
    name = name.replace("{Title}", title_str)
    name = name.replace("{Publisher}", publisher_str)
    # Keep older saved templates from leaking the deprecated token literally.
    name = name.replace("{Edition}", "")

    # Handle {Issue:03d} and {Issue} — leave blank if None
    if issue is not None:
        issue_val = int(issue) if issue == int(issue) else issue
        if "{Issue:03d}" in name:
            name = name.replace("{Issue:03d}", f"{issue_val:03d}")
        if "{Issue}" in name:
            name = name.replace("{Issue}", str(issue_val))
    else:
        name = name.replace("#{Issue:03d}", "").replace("{Issue:03d}", "")
        name = name.replace("#{Issue}", "").replace("{Issue}", "")

    # Handle {Volume:02d} and {Volume}
    if volume is not None:
        vol_val = int(volume)
        if "{Volume:02d}" in name:
            name = name.replace("{Volume:02d}", f"{vol_val:02d}")
        if "{Volume}" in name:
            name = name.replace("{Volume}", str(vol_val))
    else:
        name = name.replace("v{Volume:02d}", "").replace("{Volume:02d}", "")
        name = name.replace("v{Volume}", "").replace("{Volume}", "")

    # Clean up double spaces from missing optional tokens
    name = _collapse_whitespace(name)
    # Clean up empty parens/brackets from missing tokens
    name = re.sub(r"\(\s*\)", "", name).strip()
    name = re.sub(r"\[\s*\]", "", name).strip()
    # Clean up trailing separators
    name = _strip_trailing_separators(name).strip()

    safe_name = sanitize_for_filesystem(
        name,
        replace_illegal=replace_illegal,
        colon_replacement=colon_replacement,
    )
    return f"{safe_name}.{extension}"


def format_filename(
    series: str,
    issue_number: float,
    year: int | None = None,
    template: str = "{Series} ({Year}) #{Issue:03d}",
    extension: str = "cbz",
) -> str:
    """Format a filename from components using a naming template.

    Backward-compatible wrapper around :func:`format_comic_file` that maps
    the old ``{series}``, ``{year}``, ``{issue}`` (lowercase) tokens to the
    new ``{Series}``, ``{Year}``, ``{Issue}`` (capitalized) tokens.

    Args:
        series: Series title.
        issue_number: Issue number.
        year: Series start year (optional).
        template: Naming template with placeholders.
        extension: File extension without dot.
    """
    # Normalize old lowercase tokens to new capitalized tokens
    normalized = template
    normalized = normalized.replace("{series}", "{Series}")
    normalized = normalized.replace("{year}", "{Year}")
    normalized = normalized.replace("{issue:03d}", "{Issue:03d}")
    normalized = normalized.replace("{issue}", "{Issue}")

    return format_comic_file(
        series=series,
        year=year,
        issue=issue_number,
        template=normalized,
        extension=extension,
    )


# ---------------------------------------------------------------------------
# Preview generation for settings UI
# ---------------------------------------------------------------------------


def get_naming_preview(
    template: str,
    template_type: str = "standard",
) -> list[dict[str, str]]:
    """Generate preview examples for a naming template.

    Args:
        template: The naming template string.
        template_type: One of ``"folder"``, ``"standard"``, ``"annual"``,
            ``"non_standard"``, ``"non_standard_collection"``,
            ``"non_standard_single"`` — determines which examples are shown.

    Returns:
        List of dicts with ``"input"`` (description) and ``"output"`` (result).
    """
    results: list[dict[str, str]] = []

    for ex in PREVIEW_EXAMPLES:
        # Filter examples by template type
        if template_type == "folder":
            # Show all unique series for folder preview
            pass
        elif (
            (template_type == "standard" and ex.issue_type != "issue")
            or (template_type == "annual" and ex.issue_type != "annual")
            or (template_type == "non_standard" and ex.issue_type in ("issue", "annual"))
            or (
                template_type == "non_standard_collection"
                and not issue_type_uses_collection_template(ex.issue_type)
            )
            or (
                template_type == "non_standard_single"
                and not issue_type_uses_single_release_template(ex.issue_type)
            )
        ):
            continue

        if template_type == "folder":
            # Map issue_type to series_type for folder preview
            folder_type = ex.issue_type if ex.issue_type != "issue" else "standard"
            output = format_series_folder(
                title=ex.series,
                year=ex.year,
                publisher=ex.publisher,
                comicvine_id=ex.comicvine_id,
                series_type=folder_type,
                template=template,
            )
        else:
            output = format_comic_file(
                series=ex.series,
                year=ex.year,
                issue=ex.issue,
                volume=ex.volume,
                issue_type=ex.issue_type,
                title=ex.title,
                publisher=ex.publisher,
                template=template,
            )

        # Build a human-readable input description
        parts = [ex.series]
        if ex.year:
            parts.append(f"({ex.year})")
        if ex.issue is not None:
            parts.append(f"#{int(ex.issue) if ex.issue == int(ex.issue) else ex.issue}")
        if ex.issue_type != "issue":
            parts.append(f"[{ex.issue_type}]")

        results.append(
            {
                "input": " ".join(parts),
                "output": output,
            }
        )

    # Deduplicate folder previews by output
    if template_type == "folder":
        seen: set[str] = set()
        unique: list[dict[str, str]] = []
        for r in results:
            if r["output"] not in seen:
                seen.add(r["output"])
                unique.append(r)
        return unique[:6]  # cap at 6 for UI readability

    return results


# ---------------------------------------------------------------------------
# Filename parsing (unchanged from original)
# ---------------------------------------------------------------------------


def parse_filename(filename: str) -> ParsedFilename | None:
    """Parse a comic book filename into structured components.

    Delegates to the unified release parser and maps to the legacy
    ParsedFilename format for backward compatibility.

    .. note::
        For new code, prefer ``parse_release_title()`` from
        ``pullbox.core.release_parser`` which returns the richer
        ``ParsedRelease`` dataclass.

    Args:
        filename: The filename (with or without extension) to parse.
    """
    from pullbox.core.release_parser import parse_release_title

    parsed = parse_release_title(filename)
    if not parsed or not parsed.series_name or parsed.issue_number is None:
        return None

    return ParsedFilename(
        series=parsed.series_name,
        issue_number=parsed.issue_number,
        year=parsed.year,
        volume=parsed.volume,
        issue_type=parsed.issue_type.value,
        tags=_extract_tags(filename),
        extension=parsed.file_format,
    )


def _extract_tags(stem: str) -> list[str]:
    """Extract known tags from a filename stem."""
    return [tag for tag, pattern in _TAG_PATTERNS if pattern.search(stem)]
