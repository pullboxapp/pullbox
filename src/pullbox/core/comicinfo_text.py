"""Format-specific descriptive fields and exact IDs encountered in ComicInfo text."""

import re
from enum import StrEnum
from typing import Literal

from pullbox.core.issue_barcodes import NATIVE_BARCODES


class ColorFlag(StrEnum):
    UNKNOWN = "Unknown"
    NO = "No"
    YES = "Yes"


class MangaFlag(StrEnum):
    UNKNOWN = "Unknown"
    NO = "No"
    YES = "Yes"
    RIGHT_TO_LEFT = "YesAndRightToLeft"


FLAG_CHOICES: dict[str, tuple[str, ...]] = {
    "black_and_white": tuple(ColorFlag),
    "manga": tuple(MangaFlag),
}
type ComicInfoField = Literal[
    "review",
    "gtin",
    "scan_information",
    "black_and_white",
    "manga",
    "main_character_or_team",
    "series_groups",
    "alternate_series",
    "alternate_number",
    "alternate_count",
]
COMICINFO_ONLY_TAGS: dict[ComicInfoField, str] = {
    "review": "Review",
    "gtin": "GTIN",
    "scan_information": "ScanInformation",
    "black_and_white": "BlackAndWhite",
    "manga": "Manga",
    "main_character_or_team": "MainCharacterOrTeam",
    "series_groups": "SeriesGroup",
    "alternate_series": "AlternateSeries",
    "alternate_number": "AlternateNumber",
    "alternate_count": "AlternateCount",
}
type MetronTextField = Literal["alternative_number", "collection_title", "manga_volume"]
METRON_ONLY_TAGS: dict[MetronTextField, str] = {
    "alternative_number": "AlternativeNumber",
    "collection_title": "CollectionTitle",
    "manga_volume": "MangaVolume",
}
SINGLE_DOCUMENT: dict[str, str] = {
    **dict.fromkeys(NATIVE_BARCODES, "MetronInfo.xml"),
    "prices": "MetronInfo.xml",
    "universes": "MetronInfo.xml",
    "story_titles": "MetronInfo.xml",
    "reprints": "MetronInfo.xml",
    **dict.fromkeys(COMICINFO_ONLY_TAGS, "ComicInfo.xml"),
    **dict.fromkeys(METRON_ONLY_TAGS, "MetronInfo.xml"),
}
TEXT_LIMIT = 20000
_ISSUE_ID = re.compile(r"(?:4000-|cv_issue_id\s*:\s*)(\d+)", re.I)
_VOLUME_ID = re.compile(r"(?:4050-|cv_vol_id\s*:\s*)(\d+)", re.I)
_LEGACY_ID = re.compile(r"\[cvdb\d+\]|\b(?:cvid|issueid)[:\s]+\d+\b", re.I)
_INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def comicinfo_text_ids(text: str) -> tuple[set[str], set[str]]:
    return set(_ISSUE_ID.findall(text)), set(_VOLUME_ID.findall(text))


def descriptive_text(raw: str, *, limit: int = TEXT_LIMIT) -> str | None:
    if len(raw) > limit:
        raise ValueError(f"This field exceeds {limit:,} characters.")
    if _INVALID_XML.search(raw):
        raise ValueError("This field contains characters that XML cannot store.")
    if _LEGACY_ID.search(raw) or any(comicinfo_text_ids(raw)):
        raise ValueError("This field contains a protected comic identifier.")
    return raw.replace("\r\n", "\n").replace("\r", "\n").strip() or None


def selectable_single_line(value: object, *, limit: int = 255) -> bool:
    return value is None or (
        isinstance(value, str) and 0 < len(value) <= limit and value.isprintable()
    )


def embedded_comicinfo_value(
    field: ComicInfoField, raw: str | None
) -> str | int | tuple[str, ...] | None:
    if raw is None:
        return None
    if field == "series_groups":
        # 128 labels of 255 characters, plus their comma-space separators.
        text = descriptive_text(raw, limit=128 * 255 + 127 * 2)
        return tuple(part.strip() for part in text.split(",", 128)) if text else None
    text = descriptive_text(raw)
    if field == "alternate_count":
        if raw == "":
            return -1  # XSD default for an empty, present element, not a missing tag.
        if text and re.fullmatch(r"[+-]?[0-9]+", text):
            digits = text.lstrip("+-").lstrip("0") or "0"
            if len(digits) <= 10:
                count = int(digits) * (-1 if text.startswith("-") else 1)
                if -1 <= count <= 2147483647:
                    return count
        return raw  # Keep malformed counts visible, but not selectable.
    if field in FLAG_CHOICES:
        # Only an empty *present* element takes the XSD default. Don't repair legacy tokens.
        return raw if raw else "Unknown"
    return text
