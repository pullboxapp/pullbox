"""Unified release title parser for NZB titles and local comic filenames.

Parses structured data from both indexer release titles (NZB/torrent) and
local filenames into a single ``ParsedRelease`` dataclass.  Supports the
three dominant naming conventions in the wild:

- Square bracket: ``Batman 016 [2026] [Digital] [Shan-Empire]``
- Parenthesis:    ``Saga 054 (2018) (Digital) (Zone-Empire).cbz``
- Dot-separated:  ``Harley.Quinn.051.2025.digital.Pyrate-DCP``

Imports ``IssueType`` from ``models/issue.py`` — the only model dependency.
All functions are pure (no I/O, no database).
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from pullbox.models.issue import IssueType

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_COMIC_EXTENSIONS_RE = re.compile(r"\.(cbz|cbr|cb7|pdf|epub)$", re.IGNORECASE)

# Scan group heuristics — suffixes that identify scan/release groups
_GROUP_SUFFIXES = re.compile(r"(?:-Empire|-DCP|-HD|Empire|DCP)\b", re.IGNORECASE)

_SCAN_GROUP_CONTEXT_RE = re.compile(
    r"[\[(](?:"
    r"[Dd]igital(?:[\s-]?[Rr]ip|-?mobile|-?HD)?|[Ww]ebrip|c2c|noads|HD|"
    r"(?:AI[\s-]?)?\d{3,4}p|"
    r"Digi-Hybrid|Retail|Comic|HYBRID|HYBRiD|COMiC|COMIC|CBZ|CBR"
    r")[\])]",
    re.IGNORECASE,
)

_RESOLUTION_TAG_RE = re.compile(r"^(?:AI[\s-]?)?\d{3,4}p$", re.IGNORECASE)

_SINGLE_TOKEN_SCAN_GROUP_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.']{2,}$")

_NON_SCAN_GROUP_TAG_RE = re.compile(
    r"^(?:"
    r"\d{4}|of\s+\d+|\d+\s*covers?|"
    r"Digital(?:[\s-]?Rip|-?mobile|-?HD)?|Webrip|c2c|noads|HD|"
    r"(?:AI[\s-]?)?\d{3,4}p|"
    r"Digi-Hybrid|Retail|Comic|HYBRID|"
    r"ADULT|Collection|FRENCH|English|F|ASO|webp|CBZ|CBR|Series|"
    r"Free Comic Book Day|FCBD|Kindle edition|"
    r"Special|Original Graphic Novel|OGN|Graphic Novel|TPB|"
    r"One Shot|OS|Deluxe Edition|Annual|Omnibus|Compendium|Hardcover|HC"
    r")$",
    re.IGNORECASE,
)

# Fills / date prefixes that appear before the actual title
_FILLS_PREFIX_RE = re.compile(
    r"^(?:Fills?\s*\[\d+/\d+\]\s*|"
    r"\d{2}-\d{2}-\d{4}\s*\[\d+\s+of\s+\d+\]\s*)",
    re.IGNORECASE,
)

# Bracketed metadata tags to strip (case-insensitive content matching)
_BRACKET_TAGS_RE = re.compile(
    r"\["
    r"(?:"
    r"[Dd]igital(?:[\s-]?[Rr]ip|-?mobile|-?HD)?|[Ww]ebrip|c2c|noads|HD|"
    r"(?:AI[\s-]?)?\d{3,4}p|"
    r"Retail|Comic|HYBRID|HYBRiD|COMiC|COMIC|COMiC\.CBZ|"
    r"FRENCH|SWEDiSH|INTERNAL|iNTERNAL|English|Greek|"
    r"\d+\s*covers?|Collection|ADULT|REPACK|Repost|"
    r"Free Comic Book Day|FCBD|Kindle edition|"
    r"scan|pdf-rip|extra pages only|scanlation|"
    r"scanned physical copy|physical scan|scanned copy|"
    r"EngScanlation|WebComic|Kickstarter|"
    r"Series|F|ASO|webp|CBZ|CBR"
    r")"
    r"\]",
    re.IGNORECASE,
)

# Parenthesized metadata tags to strip
# NOTE: "Digital TPB" must NOT be stripped — TPB is a type indicator
_PAREN_TAGS_RE = re.compile(
    r"\("
    r"(?:"
    r"[Dd]igital(?:[\s-]?[Rr]ip|-?mobile|-?HD)?|[Ww]ebrip|c2c|noads|HD|"
    r"Digi-Hybrid|Retail|Comic|HYBRID|"
    r"Scanned\s+Physical\s+Copy|Physical\s+Scan|Scanned\s+Copy|"
    r"(?:AI[\s-]?)?\d{3,4}p|"
    r"\d+\s*covers?"
    r")"
    r"\)",
    re.IGNORECASE,
)

_TRAILING_RELEASE_TAG_RE = re.compile(
    r"\s*[-\u2013\u2014]\s*(?:PROPER|REPACK)\s*$",
    re.IGNORECASE,
)

# Type detection — ordered by priority (ONE_SHOT first, SPECIAL last)
# Reuses the same keyword patterns as naming.py's _TYPE_DETECTION_PATTERNS
# but with the spec's priority order and additional keywords.
_TYPE_PATTERNS: list[tuple[IssueType, re.Pattern[str]]] = [
    (IssueType.ONE_SHOT, re.compile(r"\bOne[\s-]?Shot\b|(?<!\w)OS(?!\w)", re.IGNORECASE)),
    (IssueType.ANNUAL, re.compile(r"\bAnnual\b", re.IGNORECASE)),
    (IssueType.OMNIBUS, re.compile(r"\bOmnibus\b", re.IGNORECASE)),
    (IssueType.COMPENDIUM, re.compile(r"\bCompendium\b", re.IGNORECASE)),
    (IssueType.TPB, re.compile(r"\bTPB\b|\bTrade\s+Paperback\b|\bPaperback\b", re.IGNORECASE)),
    (IssueType.HC, re.compile(r"\bHardcover\b|\b(?<!\w)HC(?!\w)\b", re.IGNORECASE)),
    (IssueType.DELUXE, re.compile(r"\bDeluxe(?:\s+Edition)?\b", re.IGNORECASE)),
    (
        IssueType.OGN,
        re.compile(r"\bOGN\b|\bOriginal\s+Graphic\s+Novel\b", re.IGNORECASE),
    ),
    (IssueType.GN, re.compile(r"\bGN\b|\bGraphic[\s.]Novel\b", re.IGNORECASE)),
    (IssueType.SPECIAL, re.compile(r"\bSpecial\b", re.IGNORECASE)),
    (IssueType.VOLUME, re.compile(r"\bVol(?:ume)?\.?\s*\d+", re.IGNORECASE)),
]

# Volume indicator patterns
_VOLUME_RE = re.compile(
    r"\b(?:Vol(?:ume)?\.?\s*(\d+)|v(\d+))\b",
    re.IGNORECASE,
)

# Year in square brackets: [2026] or [Mon YYYY]
_YEAR_BRACKET_RE = re.compile(r"\[(?:[A-Za-z]{3}\s+)?(\d{4})\]")

# Year in parentheses: (2016)
_YEAR_PAREN_RE = re.compile(r"\((\d{4})\)")

# Issue number with hash prefix: #045, #5, #5.1
_ISSUE_HASH_RE = re.compile(r"#(\d+(?:\.\d+)?)")

# DC's One Million event used the literal issue number 1,000,000 across
# multiple ongoing titles. Keep this exact exception narrow so arbitrary long
# numeric title tokens do not become issue numbers.
_DC_ONE_MILLION_ISSUE_RE = re.compile(r"(?<=\s)(1000000)(?=\s|$)")

# Long-running UK weekly anthologies often label issue numbers as "Prog 2483".
_PROG_ISSUE_RE = re.compile(r"\bProg(?:ramme)?\.?\s*#?\s*(\d+(?:\.\d+)?)\b", re.IGNORECASE)

# Limited series marker: (of 05)
_LIMITED_SERIES_RE = re.compile(r"\(of\s+\d+\)", re.IGNORECASE)
_INLINE_LIMITED_SERIES_RE = re.compile(
    r"(?<![\w.])(\d{1,3}(?:\.\d+)?)\s+of\s+\d{1,3}\b",
    re.IGNORECASE,
)

# Pack range pattern: 001-010 (but NOT year ranges like 1998-2009)
_PACK_RANGE_RE = re.compile(r"\b(\d{1,3})\s*-\s*(\d{1,3})\b")

# Publisher prefixes in dot-separated format
_PUBLISHER_PREFIX_RE = re.compile(
    r"^(?:DC\.Comics|Marvel|Image\.Comics|Dark\.Horse|IDW\.Publishing|"
    r"BOOM\.Studios|Dynamite|Oni\.Press|Titan\.Comics|Yen\.Press|"
    r"Archie|Fantagraphics|AWA\.Studios|Tidalwave\.Productions|"
    r"Z2\.Comics|Noir\.Caesar|Disney|Humanoids|"
    r"Valiant|Viz\.Media|Kodansha(?:\.Comics)?|AfterShock(?:\.Comics)?|"
    r"Mad\.Cave(?:\.Studios)?|Ablaze|Vault\.Comics|DSTLRY|"
    r"Ahoy\.Comics|Scout\.Comics|TKO\.Studios|Antarctic\.Press|"
    r"Rebellion|Top\.Cow|Lion\.Forge|Zenescope|"
    r"Avatar\.Press|Action\.Lab|Aspen(?:\.Comics)?|Vertigo|"
    r"Black\.Mask|Magnetic\.Press|Papercutz)[.-]",
    re.IGNORECASE,
)

_PAREN_KNOWN_PUBLISHER_RE = re.compile(
    r"\(\s*(?:"
    r"DC(?:\s+Comics)?|Marvel(?:\s+Comics)?|Image(?:\s+Comics)?|"
    r"Dark\s+Horse(?:\s+Comics)?|IDW(?:\s+Publishing)?|"
    r"BOOM!?(?:\s+Studios)?|Dynamite(?:\s+Entertainment)?|"
    r"Oni(?:\s+Press)?|Titan(?:\s+Comics)?|Yen(?:\s+Press)?|"
    r"Archie|Fantagraphics|AWA(?:\s+Studios)?|"
    r"Tidalwave(?:\s+Productions)?|Z2(?:\s+Comics)?|"
    r"Noir\s+Caesar|Disney|Humanoids|Valiant|Viz(?:\s+Media)?|"
    r"Kodansha(?:\s+Comics)?|AfterShock(?:\s+Comics)?|"
    r"Mad\s+Cave(?:\s+Studios)?|Ablaze|Vault(?:\s+Comics)?|"
    r"DSTLRY|Ahoy(?:\s+Comics)?|Scout(?:\s+Comics)?|"
    r"TKO(?:\s+Studios)?|Antarctic(?:\s+Press)?|Rebellion|"
    r"Top\s+Cow|Lion\s+Forge|Zenescope|Avatar(?:\s+Press)?|"
    r"Action\s+Lab|Aspen(?:\s+Comics)?|Vertigo|Black\s+Mask|"
    r"Magnetic(?:\s+Press)?|Papercutz|Harper\s*Collins|CrossGen"
    r")\s*\)",
    re.IGNORECASE,
)

_PAREN_KNOWN_PUBLISHER_YEAR_RE = re.compile(
    r"\(\s*(?:"
    r"DC(?:\s+Comics)?|Marvel(?:\s+Comics)?|Image(?:\s+Comics)?|"
    r"Dark\s+Horse(?:\s+Comics)?|IDW(?:\s+Publishing)?|"
    r"BOOM!?(?:\s+Studios)?|Dynamite(?:\s+Entertainment)?|"
    r"Oni(?:\s+Press)?|Titan(?:\s+Comics)?|Yen(?:\s+Press)?|"
    r"Archie|Fantagraphics|AWA(?:\s+Studios)?|"
    r"Tidalwave(?:\s+Productions)?|Z2(?:\s+Comics)?|"
    r"Noir\s+Caesar|Disney|Humanoids|Valiant|Viz(?:\s+Media)?|"
    r"Kodansha(?:\s+Comics)?|AfterShock(?:\s+Comics)?|"
    r"Mad\s+Cave(?:\s+Studios)?|Ablaze|Vault(?:\s+Comics)?|"
    r"DSTLRY|Ahoy(?:\s+Comics)?|Scout(?:\s+Comics)?|"
    r"TKO(?:\s+Studios)?|Antarctic(?:\s+Press)?|Rebellion|"
    r"Top\s+Cow|Lion\s+Forge|Zenescope|Avatar(?:\s+Press)?|"
    r"Action\s+Lab|Aspen(?:\s+Comics)?|Vertigo|Black\s+Mask|"
    r"Magnetic(?:\s+Press)?|Papercutz|Harper\s*Collins|CrossGen"
    r")\s+(?P<year>(?:19|20)\d{2})\s*\)",
    re.IGNORECASE,
)

# "No." or "No" issue number in dot-separated: No.01, No.02
_DOT_NO_ISSUE_RE = re.compile(r"\bNo\.?\s*(\d+)\b", re.IGNORECASE)
_SCENE_PREFIX_NUMBERED_RE = re.compile(
    r"^(?P<group>[a-z][a-z0-9]{1,15})-"
    r"(?P<title>[A-Z].*?[._\s]+(?i:No)\.?[._\s]*\d{1,5})\s*$"
)

# Minimal separator-only filenames such as ``dc_connect_72.pdf`` are common
# enough to support, but this path stays narrower than full dot release parsing.
_MINIMAL_SEPARATOR_ISSUE_RE = re.compile(r"^(?P<series>.+?)[._](?P<issue>0*\d{1,3}(?:\.\d+)?)$")
_GENERIC_MINIMAL_SERIES_TOKENS = frozenset(
    {"scan", "scans", "page", "pages", "img", "image", "images", "cover", "covers", "comic"}
)

# Type keywords to strip from series name after detection
_TYPE_STRIP_RE = re.compile(
    r"\s*\b(?:Annual|TPB|Trade\s+Paperback|Paperback|"
    r"Omnibus|Compendium|Original\s+Graphic\s+Novel|OGN|Graphic[\s.]Novel|GN|"
    r"Hardcover|HC|One[\s-]?Shot|OS|Special(?:\s+Edition)?|"
    r"Deluxe(?:\s+Edition)?|Vol(?:ume)?\.?\s*\d+(?:\s*:[^()\[\]]*)?)\b\s*",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedRelease:
    """Unified parsing result for NZB titles and local filenames."""

    original_title: str
    series_name: str | None
    issue_number: float | None
    year: int | None
    volume: str | None
    issue_type: IssueType
    scan_group: str | None
    file_format: str | None
    is_pack: bool
    pack_range: str | None


# ---------------------------------------------------------------------------
# Issue number utilities
# ---------------------------------------------------------------------------


def normalize_issue_number(raw: str | float | None) -> float | None:
    """Normalize an issue number to a float.

    Handles leading zeros, alpha suffixes, and unicode fractions.

    Args:
        raw: Issue number as string, float, or None.

    Returns:
        Normalized float or None if unparseable.
    """
    if raw is None:
        return None

    if isinstance(raw, int | float):
        return float(raw)

    text = str(raw).strip()
    if not text:
        return None

    # Strip leading hash: "#5" → "5"
    if text.startswith("#"):
        text = text[1:]

    limited_series_match = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*(?:[\[(]\s*of\s+\d+\s*[\])]|\s+of\s+\d+)",
        text,
        re.IGNORECASE,
    )
    if limited_series_match:
        text = limited_series_match.group(1)

    # Unicode fraction handling
    fraction_map = {"½": 0.5, "¼": 0.25, "¾": 0.75, "⅓": 1 / 3, "⅔": 2 / 3}
    if text in fraction_map:
        return fraction_map[text]

    # ASCII fraction handling: "1/2" → 0.5
    frac_match = re.match(r"^(\d+)/(\d+)$", text)
    if frac_match:
        num, denom = int(frac_match.group(1)), int(frac_match.group(2))
        if denom != 0:
            return num / denom

    # Strip alpha suffixes: "5a" → "5", "12AU" → "12"
    cleaned = re.sub(r"[a-zA-Z]+$", "", text).strip()
    if not cleaned:
        return None

    try:
        return float(cleaned)
    except ValueError:
        return None


def issues_match(wanted: float, found: float | None, tolerance: float = 0.001) -> bool:
    """Check if two issue numbers match within floating-point tolerance.

    Args:
        wanted: The issue number we're looking for.
        found: The issue number from a release (may be None).
        tolerance: Maximum allowed difference for float comparison.
    """
    if found is None:
        return False
    return abs(wanted - found) < tolerance


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------


def parse_release_title(title: str) -> ParsedRelease | None:
    """Parse an NZB release title or local filename into structured components.

    Handles all common naming conventions:
    - NZB bracket: ``Batman 016 [2026] [Digital] [Shan-Empire]``
    - NZB dot-sep: ``Harley.Quinn.051.2025.digital.Pyrate-DCP``
    - Local paren:  ``Batman (2016) #045 (Digital) (Zone-Empire).cbz``
    - Local minimal: ``Batman 045.cbz``

    Returns None only for empty/whitespace-only input.
    """
    if not title or not title.strip():
        return None

    working = title.strip()

    # Step a: Strip file extension (and any trailing torrent metadata after it)
    file_format = _extract_extension(working)
    if file_format:
        working = _COMIC_EXTENSIONS_RE.sub("", working).strip()
    else:
        # Extension may not be at end — torrent titles can have tracker tags
        # after it, e.g. "...Empire).cbr [ettv] ( Nem )".  Truncate at the
        # extension and discard everything after it.
        mid_ext = re.search(r"\.(cbz|cbr|cb7|pdf|epub)\b", working, re.IGNORECASE)
        if mid_ext:
            file_format = mid_ext.group(1).lower()
            working = working[: mid_ext.start()].strip()
    # Handle dot-sep titles ending in .COMIC, .CBZ etc (not real extensions)
    working = re.sub(r"\.(?:COMIC|COMiC|Comic)$", "", working, flags=re.IGNORECASE).strip()

    # Strip Fills prefix and date prefixes
    working = _FILLS_PREFIX_RE.sub("", working).strip()

    # Early exit after extension stripping
    if not working:
        return None

    # Step b: Extract scan group (BEFORE normalizing separators)
    scan_group, working = _extract_scan_group(working)

    # Some metadata sources retain a short lowercase scene-group prefix in
    # otherwise explicit ``Series.No.7`` filenames. Keep the rule narrow so
    # ordinary hyphenated series titles remain untouched.
    scene_match = _SCENE_PREFIX_NUMBERED_RE.fullmatch(working)
    if scene_match:
        scan_group = scan_group or scene_match.group("group")
        working = scene_match.group("title")

    # Step c: Normalize separators (dots → spaces for dot-separated format)
    is_dot_separated = _is_dot_format(working) or _is_minimal_separator_issue_format(working)
    if is_dot_separated:
        # Strip publisher prefix first
        working = _PUBLISHER_PREFIX_RE.sub("", working).strip()
        working = _normalize_dot_separated(working)

    # Step d: Extract bracketed/parenthesized metadata tags
    working = _BRACKET_TAGS_RE.sub("", working).strip()
    working = _PAREN_TAGS_RE.sub("", working).strip()
    working = _TRAILING_RELEASE_TAG_RE.sub("", working).strip()
    # Strip "Digital TPB" parens — remove only the "Digital" part, keep "TPB" visible
    working = re.sub(r"\(\s*Digital\s+TPB\s*\)", "(TPB)", working)
    # Strip any remaining format tags like FRENCH, HYBRiD, digital, etc.
    working = re.sub(
        r"\b(?:FRENCH|SWEDiSH|HYBRID|HYBRiD|INTERNAL|iNTERNAL|English|Greek|"
        r"[Dd]igital|[Rr]etail|Repost)\b",
        "",
        working,
    ).strip()

    # Step e: Detect issue type
    issue_type = _detect_type(working)

    # Step f: Extract year
    year, working = _extract_year(working)

    # Step g: Extract volume
    volume, working = _extract_volume(working)

    # Step g½: Detect pack ranges before issue extraction so a range like
    # "001 - 100" does not accidentally become issue #100.
    pre_issue_is_pack, pre_issue_pack_range = _detect_pack(working)

    # Step h: Extract issue number
    issue_number, working = _extract_issue_number(working, issue_type)

    # Step h½: Reclassify as VOLUME when volume is present but no issue
    # number was found and no other type was explicitly detected.
    # e.g. "Batman v01" → VOLUME, but "Spider-Man v2 015" stays ISSUE.
    if issue_type == IssueType.ISSUE and volume is not None and issue_number is None:
        issue_type = IssueType.VOLUME

    # Step i: Detect pack ranges
    post_issue_is_pack, post_issue_pack_range = _detect_pack(working)
    is_pack = pre_issue_is_pack or post_issue_is_pack
    pack_range = pre_issue_pack_range or post_issue_pack_range

    # Step j: Series name cleanup
    series_name = _clean_series_name(working, issue_type)

    return ParsedRelease(
        original_title=title,
        series_name=series_name or None,
        issue_number=issue_number,
        year=year,
        volume=volume,
        issue_type=issue_type,
        scan_group=scan_group,
        file_format=file_format,
        is_pack=is_pack,
        pack_range=pack_range,
    )


# ---------------------------------------------------------------------------
# Pipeline step implementations
# ---------------------------------------------------------------------------


def _extract_extension(title: str) -> str | None:
    """Step a: Extract and return file extension, or None."""
    m = _COMIC_EXTENSIONS_RE.search(title)
    return m.group(1).lower() if m else None


def _extract_scan_group(title: str) -> tuple[str | None, str]:
    """Step b: Extract scan group from the title.

    Returns (group_name, remaining_title).
    """
    # Pattern 1: Last [bracketed] item that looks like a scan group
    # Work backwards through bracket groups
    brackets = list(re.finditer(r"\[([^\]]+)\]", title))
    if brackets:
        last = brackets[-1]
        content = last.group(1)
        # Check if it looks like a scan group (contains group suffix or is
        # a known pattern like "name-name")
        prefix = title[: last.start()]
        if (
            _GROUP_SUFFIXES.search(content)
            or _looks_like_contextual_scan_group(
                content,
                prefix,
            )
            or (
                "-" in content
                and not re.match(r"^\d{4}$", content)
                and not re.match(r"^(?:Digital|Webrip|c2c|HD|Retail|One[\s-]?Shot)", content, re.I)
                and not _RESOLUTION_TAG_RE.match(content)
                and not re.match(r"^\d+\s*covers?$", content, re.I)
                and not re.match(
                    r"^(?:ADULT|Collection|FRENCH|English|F|ASO|webp|CBZ|Series)$", content, re.I
                )
                and not re.match(r"^(?:Free Comic Book Day|FCBD|Kindle edition)$", content, re.I)
                and not re.match(
                    (
                        r"^(?:Special|Original Graphic Novel|OGN|Graphic Novel|TPB|"
                        r"One Shot|Deluxe Edition)$"
                    ),
                    content,
                    re.I,
                )
            )
        ):
            remaining = title[: last.start()].rstrip() + title[last.end() :]
            return content, remaining.strip()

    # Pattern 2: Last (parenthesized) item with group suffix or hyphenated name
    parens = list(re.finditer(r"\(([^)]+)\)", title))
    if parens:
        last = parens[-1]
        content = last.group(1)
        prefix = title[: last.start()]
        # Match if it has known group suffixes, or if it looks like a hyphenated
        # group name (e.g., "Minutemen-Spaztastic", "Son of Ultron-Empire")
        is_group = (
            _GROUP_SUFFIXES.search(content)
            or _looks_like_contextual_scan_group(content, prefix)
            or (
                "-" in content
                and not re.match(r"^\d{4}$", content)
                and not re.match(r"^(?:of\s+\d+)$", content, re.I)
                and not re.match(r"^\d+\s*covers?$", content, re.I)
                and not re.match(r"^(?:Digital|c2c|noads|Digi-Hybrid)$", content, re.I)
                and not _RESOLUTION_TAG_RE.match(content)
            )
        )
        if is_group:
            remaining = title[: last.start()].rstrip() + title[last.end() :]
            return content, remaining.strip()

    # Pattern 3: Trailing -GroupName in dot-separated format
    # e.g., "...digital.Pyrate-DCP" → group = "Pyrate-DCP"
    m = re.search(r"[.\s](\w+(?:-\w+)+)$", title)
    if m and _GROUP_SUFFIXES.search(m.group(1)):
        return m.group(1), title[: m.start()].strip()

    # Pattern 4: Trailing -GroupName with dot suffix like ".Resin" or ".tomjoad"
    m = re.search(r"[.\s](\w[\w\s]*(?:-\w+)*)\.\s*(\w+)\s*$", title)
    if m:
        potential = f"{m.group(1)}.{m.group(2)}"
        if _GROUP_SUFFIXES.search(potential) or re.search(r"-\w+\.\s*\w+$", potential):
            pass  # Skip ambiguous cases

    return None, title


def _looks_like_contextual_scan_group(content: str, prefix: str) -> bool:
    """Return true for single-token scanner tags following release metadata."""
    cleaned = content.strip()
    if not _SINGLE_TOKEN_SCAN_GROUP_RE.match(cleaned):
        return False
    if _NON_SCAN_GROUP_TAG_RE.match(cleaned):
        return False
    return bool(_SCAN_GROUP_CONTEXT_RE.search(prefix))


def _is_dot_format(title: str) -> bool:
    """Detect if the title uses dot-separated or underscore-separated naming."""
    # Count separator chars vs spaces
    dots = title.count(".")
    underscores = title.count("_")
    spaces = title.count(" ")
    separators = dots + underscores
    # Must have at least a few separators and more separators than spaces
    return separators >= 3 and separators > spaces


def _normalize_dot_separated(title: str) -> str:
    """Step c: Convert dot-separated format to spaces.

    Preserves intentional patterns like version numbers and abbreviations.
    """
    # Replace dots with spaces
    result = title.replace(".", " ")
    # Replace underscores with spaces
    result = result.replace("_", " ")
    # Collapse multiple spaces
    result = re.sub(r"\s{2,}", " ", result).strip()
    return result


def _is_minimal_separator_issue_format(title: str) -> bool:
    """Return true for narrow ``Series_Name_001`` style local filenames."""
    if " " in title or ("_" not in title and "." not in title):
        return False
    if title.count("_") + title.count(".") < 2:
        return False

    match = _MINIMAL_SEPARATOR_ISSUE_RE.match(title)
    if not match:
        return False

    raw_issue = match.group("issue")
    if re.fullmatch(r"(?:19|20)\d{2}", raw_issue):
        return False
    issue_number = normalize_issue_number(raw_issue)
    if issue_number is None or issue_number <= 0 or issue_number >= 500:
        return False

    normalized_series = _normalize_dot_separated(match.group("series"))
    alpha_tokens = [
        token.casefold()
        for token in re.split(r"\s+", normalized_series)
        if re.search(r"[A-Za-z]", token)
    ]
    if len(alpha_tokens) < 2:
        return False
    return not all(token in _GENERIC_MINIMAL_SERIES_TOKENS for token in alpha_tokens)


def _detect_type(title: str) -> IssueType:
    """Step e: Detect issue type from the title.

    Checks keywords in priority order (ONE_SHOT > ANNUAL > ... > SPECIAL).
    """
    for issue_type, pattern in _TYPE_PATTERNS:
        if pattern.search(title):
            return issue_type
    return IssueType.ISSUE


def _extract_year(title: str) -> tuple[int | None, str]:
    """Step f: Extract year from the title.

    Priority: [YYYY] brackets > (YYYY) parens > standalone.
    Returns (year, remaining_title).
    """
    # Priority 1: [YYYY] or [Mon YYYY] in square brackets
    m = _YEAR_BRACKET_RE.search(title)
    if m:
        year = int(m.group(1))
        if 1900 <= year <= 2099:
            remaining = title[: m.start()] + title[m.end() :]
            return year, remaining.strip()

    # Priority 2: (Known Publisher YYYY) in parentheses
    m = _PAREN_KNOWN_PUBLISHER_YEAR_RE.search(title)
    if m:
        year = int(m.group("year"))
        if 1900 <= year <= 2099:
            remaining = title[: m.start()] + title[m.end() :]
            return year, remaining.strip()

    # Priority 3: (YYYY) in parentheses
    m = _YEAR_PAREN_RE.search(title)
    if m:
        year = int(m.group(1))
        if 1900 <= year <= 2099:
            remaining = title[: m.start()] + title[m.end() :]
            return year, remaining.strip()

    # Priority 4: Standalone year after series content (not part of series name).
    # Dotted single-file names like "Friendo.2019.pdf" reach this step as
    # "Friendo.2019", so treat a dot separator like whitespace for realistic
    # publication years. Leave far-future dotted names such as Spider-Man.2099
    # alone because those are usually part of the title.
    for m in re.finditer(r"(?P<sep>[\s.])(?P<year>\d{4})(?=[\s.]|$)", title):
        year = int(m.group("year"))
        if m.group("sep") == "." and year > 2039:
            continue
        if 1940 <= year <= 2099:
            remaining = title[: m.start("sep")] + title[m.end() :]
            return year, remaining.strip()

    return None, title


def _extract_volume(title: str) -> tuple[str | None, str]:
    """Step g: Extract volume indicator.

    Returns (volume_string, remaining_title).
    Disambiguates v2016 as year (if >= 1940) vs volume (if < 1940).
    """
    m = _VOLUME_RE.search(title)
    if m:
        vol_num_str = m.group(1) or m.group(2)
        vol_num = int(vol_num_str)

        # v2016 where N >= 1940 is likely a year, not volume
        if vol_num >= 1940:
            return None, title

        vol_text = m.group(0)
        end = m.end()
        # Consume optional colon+subtitle after volume number
        # e.g. "Vol.1:Court of Owls" → strip ":Court of Owls" too
        colon_sub = re.match(r"\s*:[^()\[\]]+", title[end:])
        if colon_sub:
            end += colon_sub.end()
        remaining = title[: m.start()] + title[end:]
        return vol_text.strip(), remaining.strip()

    return None, title


_WORD_NUMBERS: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}

_WORD_NUMBER_RE = re.compile(
    r"\b(?:Book|Part|Volume|Vol\.?)\s+(" + "|".join(_WORD_NUMBERS) + r")\b",
    re.IGNORECASE,
)


def _extract_issue_number(title: str, issue_type: IssueType) -> tuple[float | None, str]:
    """Step h: Extract issue number from the title.

    Returns (issue_number, remaining_title).
    """
    # Remove limited series markers first: (of 05)
    clean = _LIMITED_SERIES_RE.sub("", title).strip()

    # Priority 0: Word numbers — "Book Two", "Part Three", "Volume One"
    m = _WORD_NUMBER_RE.search(clean)
    if m:
        num = float(_WORD_NUMBERS[m.group(1).lower()])
        remaining = clean[: m.start()] + clean[m.end() :]
        return num, remaining.strip()

    # Priority 1: Hash prefix — #045, #5, #5.1
    m = _ISSUE_HASH_RE.search(clean)
    if m:
        num = float(m.group(1))
        remaining = clean[: m.start()] + clean[m.end() :]
        return num, remaining.strip()

    # Priority 2: DC One Million's literal issue 1,000,000 without a hash.
    m = _DC_ONE_MILLION_ISSUE_RE.search(clean)
    if m:
        num = float(m.group(1))
        remaining = clean[: m.start()] + clean[m.end() :]
        return num, remaining.strip()

    # Priority 3: Anthology "Prog" marker — supports long-running series
    # whose issue numbers exceed the usual 2-3 digit positional heuristic.
    m = _PROG_ISSUE_RE.search(clean)
    if m:
        num = float(m.group(1))
        remaining = clean[: m.start()] + clean[m.end() :]
        return num, remaining.strip()

    # Priority 4: "No." pattern from dot-separated titles
    m = _DOT_NO_ISSUE_RE.search(clean)
    if m:
        num = float(m.group(1))
        remaining = clean[: m.start()] + clean[m.end() :]
        return num, remaining.strip()

    # Priority 5: Inline limited-series marker — "02 of 03".  The first number
    # is the issue; the second is the total issue count.
    m = _INLINE_LIMITED_SERIES_RE.search(clean)
    if m:
        num = float(m.group(1))
        remaining = clean[: m.start()] + clean[m.end() :]
        return num, remaining.strip()

    # Priority 6: Positional number — a 2-3 digit number that sits between
    # the series name and metadata (year/brackets)
    # Match a number preceded by space (or after series-name text)
    # but NOT part of an alphanumeric word like "D4VE2" or "Spider-Man 2099"
    positional_matches = list(
        re.finditer(
            r"(?<=\s)(\d{2,3})(?:\.\d+)?(?=\s|$)",
            clean,
        )
    )
    m = _select_positional_issue_match(positional_matches)
    if m:
        num_str = m.group(0)
        num = float(num_str)
        # Avoid treating large numbers that could be years as issue numbers
        if num < 500:
            remaining = clean[: m.start()] + clean[m.end() :]
            return num, remaining.strip()
        # If num >= 500, check if it might still be a valid issue (e.g., long-running series)
        # but only if there's no year already found and the number doesn't look like a year
        if num >= 1900:
            return None, clean
        # Numbers 500-1899 — treat as issue for very long-running series
        remaining = clean[: m.start()] + clean[m.end() :]
        return num, remaining.strip()

    # Priority 7: Single digit number at word boundary after text
    # Must NOT be followed by a word (e.g. "4 Covers" is a count, not issue #4)
    m = re.search(r"(?<=\s)(\d)(?=\s|$)", clean)
    if m:
        # Check the word after the digit — if it's alphabetic, this is likely
        # a count or descriptor (e.g. "4 Covers", "3 Stories"), not an issue number
        after = clean[m.end() :].lstrip()
        if after and after[0].isalpha():
            pass  # skip — looks like "N <word>", not an issue number
        else:
            num = float(m.group(1))
            remaining = clean[: m.start()] + clean[m.end() :]
            return num, remaining.strip()

    return None, clean


def _select_positional_issue_match(matches: list[re.Match[str]]) -> re.Match[str] | None:
    """Prefer a zero-padded trailing issue over numeric title tokens."""
    if not matches:
        return None
    if len(matches) > 1:
        tail_token = matches[-1].group(1)
        if tail_token.startswith("0") and len(tail_token) > 1:
            return matches[-1]
    return matches[0]


def _detect_pack(title: str) -> tuple[bool, str | None]:
    """Step i: Detect pack ranges like 001-010."""
    m = _PACK_RANGE_RE.search(title)
    if m:
        start, end = int(m.group(1)), int(m.group(2))
        # Avoid year ranges (both sides >= 1900)
        if start >= 1900 and end >= 1900:
            return False, None
        # Avoid "Season 2.5" type patterns (already handled)
        if end > start and end - start > 0:
            return True, f"{start}-{end}"
    return False, None


def _clean_series_name(title: str, issue_type: IssueType) -> str | None:
    """Step j: Clean up remaining text as the series name."""
    name = title

    # Strip type keywords from name if we detected a non-ISSUE type
    if issue_type != IssueType.ISSUE:
        name = _TYPE_STRIP_RE.sub(" ", name)

    # Strip standalone recent years that remain in the series name (from
    # patterns like "Absolute Batman 2025 Annual" where year was extracted).
    # Only strip years >= 2000 that appear as standalone tokens (not part of
    # a series name like "Spider-Man 2099").
    name = re.sub(r"(?<!\w)(20[0-3]\d)(?!\w)(?!\s+AD\b)", "", name, flags=re.IGNORECASE)

    # Decode HTML entities: &amp; → &
    name = html.unescape(name)

    # Clean up empty brackets/parens left after stripping
    name = re.sub(r"\(\s*\)", "", name)
    name = re.sub(r"\[\s*\]", "", name)

    # Strip remaining bracketed items that aren't part of the title
    # (publisher names, leftover tags)
    name = re.sub(r"\[[^\]]*\]", "", name)

    # Strip remaining parenthesized items that look like metadata
    # (but preserve parens that are part of series names)
    name = re.sub(r"\([^)]*(?:Comics?|Press|Books?|Studios?|Published)\)", "", name, flags=re.I)
    name = _PAREN_KNOWN_PUBLISHER_RE.sub("", name)

    # Strip count descriptors like "4 Covers", "3 Stories", "2 Printings"
    name = re.sub(
        r"\b\d+\s+(?:Covers?|Stories|Printings?|Variants?|Editions?)\b", "", name, flags=re.I
    )

    # Strip lingering format/metadata words (and trailing group fragments
    # like "eBook-BitBook" that weren't caught by scan group extraction).
    name = (
        re.sub(r"\b\w+-\w+$", "", name)
        if re.search(r"\b(?:eBook|Digital|Hybrid|Retail|Comic)\w*-\w+$", name, re.I)
        else name
    )
    name = re.sub(r"\b(?:HD|Retail|Repost|Hybrid|eBook|Digital|Webrip|c2c)\b", "", name, flags=re.I)
    name = re.sub(r"\bCOMIC\b(?=\s*$)", "", name)
    name = re.sub(r"\s*-\s*Issue\s*$", "", name, flags=re.I)

    # Late metadata stripping can turn "(Webrip)"-style leftovers into "()".
    name = re.sub(r"\(\s*\)", "", name)
    name = re.sub(r"\[\s*\]", "", name)

    # Strip trailing orphaned fragments left after format word stripping
    # (e.g. "Alien Bloodlines -BitBook" → "Alien Bloodlines").
    name = re.sub(r"\s+-\w+$", "", name)

    # Strip leading/trailing whitespace, hyphens, dashes
    name = name.strip()
    name = re.sub(r"^[\s\-\u2013\u2014]+", "", name)
    name = re.sub(r"[\s\-\u2013\u2014]+$", "", name)

    # Collapse multiple spaces
    name = re.sub(r"\s{2,}", " ", name).strip()

    # Strip trailing punctuation cruft
    name = re.sub(r"[\s,:\-.]+$", "", name).strip()

    # Strip leading "DC-", "Marvel-" publisher prefixes in hyphenated format
    # but only if there's still substantial text after
    m = re.match(
        r"^(?:DC|Marvel|Image|Dark Horse|IDW|Dynamite|BOOM|Oni|Titan|"
        r"Humanoids|Valiant|Ablaze|DSTLRY|Zenescope|Vertigo|"
        r"AfterShock|Aspen|Rebellion|Avatar|Lion Forge|Top Cow|"
        r"Viz Media|Kodansha|Mad Cave|Vault|Ahoy|Scout|TKO|"
        r"Action Lab|Magnetic Press|Papercutz)\s*-\s*",
        name,
        re.IGNORECASE,
    )
    if m and len(name) > m.end() + 3:
        name = name[m.end() :]

    return name if name else None
