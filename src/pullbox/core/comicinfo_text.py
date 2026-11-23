"""Format-specific descriptive fields and exact IDs encountered in ComicInfo text."""

import re
from enum import StrEnum
from typing import Literal


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
type ComicInfoField = Literal["review", "scan_information", "black_and_white", "manga"]
COMICINFO_ONLY_TAGS: dict[ComicInfoField, str] = {
    "review": "Review",
    "scan_information": "ScanInformation",
    "black_and_white": "BlackAndWhite",
    "manga": "Manga",
}
TEXT_LIMIT = 20000
_ISSUE_ID = re.compile(r"(?:4000-|cv_issue_id\s*:\s*)(\d+)", re.I)
_VOLUME_ID = re.compile(r"(?:4050-|cv_vol_id\s*:\s*)(\d+)", re.I)
_LEGACY_ID = re.compile(r"\[cvdb\d+\]|\b(?:cvid|issueid)[:\s]+\d+\b", re.I)
_INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def comicinfo_text_ids(text: str) -> tuple[set[str], set[str]]:
    return set(_ISSUE_ID.findall(text)), set(_VOLUME_ID.findall(text))


def descriptive_text(raw: str) -> str | None:
    if len(raw) > TEXT_LIMIT:
        raise ValueError("This field exceeds 20,000 characters.")
    if _INVALID_XML.search(raw):
        raise ValueError("This field contains characters that XML cannot store.")
    if _LEGACY_ID.search(raw) or any(comicinfo_text_ids(raw)):
        raise ValueError("This field contains a protected comic identifier.")
    return raw.replace("\r\n", "\n").replace("\r", "\n").strip() or None


def embedded_comicinfo_value(field: ComicInfoField, raw: str | None) -> str | None:
    if raw is None:
        return None
    text = descriptive_text(raw)
    if field in FLAG_CHOICES:
        # Only an empty *present* element takes the XSD default. Don't repair legacy tokens.
        return raw if raw else "Unknown"
    return text
