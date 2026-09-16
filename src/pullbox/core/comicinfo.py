"""ComicInfo.xml parser.

Extracts metadata from the ComicInfo.xml standard used inside CBZ/CBR/CB7
archives.  Handles malformed or partial XML gracefully — missing elements
are returned as ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree

import structlog
from defusedxml import ElementTree as DefusedElementTree
from defusedxml.common import DefusedXmlException

from pullbox.core.comicinfo_sanitizer import scrub_stale_retailer_value

logger = structlog.get_logger(__name__)


@dataclass
class ComicInfoData:
    """Parsed data from a ComicInfo.xml file."""

    series: str | None = None
    number: str | None = None
    volume: str | None = None
    title: str | None = None
    year: int | None = None
    month: int | None = None
    day: int | None = None
    publisher: str | None = None
    notes: str | None = None
    summary: str | None = None
    writer: str | None = None
    penciller: str | None = None
    inker: str | None = None
    colorist: str | None = None
    letterer: str | None = None
    cover_artist: str | None = None
    editor: str | None = None
    page_count: int | None = None
    genre: str | None = None
    web: str | None = None
    story_arc: str | None = None
    story_arc_number: str | None = None
    series_group: str | None = None
    language: str | None = None


def parse_comicinfo(xml_string: str) -> ComicInfoData:
    """Parse a ComicInfo.xml string into a ``ComicInfoData`` instance.

    Gracefully handles malformed XML by returning a default (all-None)
    ``ComicInfoData``.  Individual missing elements are simply left as
    ``None``.

    Args:
        xml_string: Raw XML content of a ComicInfo.xml file.
    """
    try:
        root = DefusedElementTree.fromstring(xml_string)
    except (ElementTree.ParseError, DefusedXmlException) as exc:
        logger.warning("comicinfo_parse_error", error=str(exc))
        return ComicInfoData()

    return ComicInfoData(
        series=_text(root, "Series"),
        number=_text(root, "Number"),
        volume=_text(root, "Volume"),
        title=_text(root, "Title"),
        year=_int(root, "Year"),
        month=_int(root, "Month"),
        day=_int(root, "Day"),
        publisher=_text(root, "Publisher"),
        notes=_trusted_text(root, "Notes"),
        summary=_text(root, "Summary"),
        writer=_text(root, "Writer"),
        penciller=_text(root, "Penciller"),
        inker=_text(root, "Inker"),
        colorist=_text(root, "Colorist"),
        letterer=_text(root, "Letterer"),
        cover_artist=_text(root, "CoverArtist"),
        editor=_text(root, "Editor"),
        page_count=_int(root, "PageCount"),
        genre=_text(root, "Genre"),
        web=_trusted_text(root, "Web"),
        story_arc=_text(root, "StoryArc"),
        story_arc_number=_text(root, "StoryArcNumber"),
        series_group=_text(root, "SeriesGroup"),
        language=_text(root, "LanguageISO"),
    )


def _text(root: ElementTree.Element, tag: str) -> str | None:
    """Get the text content of a child element, or None."""
    el = root.find(tag)
    if el is not None and el.text:
        return el.text.strip()
    return None


def _trusted_text(root: ElementTree.Element, tag: str) -> str | None:
    """Return trusted ComicInfo text, ignoring stale retailer metadata."""
    value = _text(root, tag)
    trusted = scrub_stale_retailer_value(value)
    return trusted if isinstance(trusted, str) else None


def _int(root: ElementTree.Element, tag: str) -> int | None:
    """Get the integer text content of a child element, or None."""
    value = _text(root, tag)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None
