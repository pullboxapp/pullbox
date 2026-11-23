"""The exact shared subset of ComicInfo and MetronInfo community scores."""

import re
from xml.etree import ElementTree as ET

RATING_HELP = "Use a score from 0 to 5 with up to two decimal places."
RATING_LOCKED = (
    "This community rating contains additional XML content. Leave it unchanged or correct "
    "the source before editing the score."
)


def shared_score(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 128:
        return None
    raw = value.strip()
    if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", raw):
        return None
    integer, _, fractional = raw.lstrip("+-").partition(".")
    whole = integer.lstrip("0") or "0"
    fraction = fractional.rstrip("0")
    if (
        len(whole) != 1
        or whole > "5"
        or (whole == "5" and fraction)
        or len(fraction) > 2
        or (raw.startswith("-") and (whole != "0" or fraction))
    ):
        return None
    return f"{whole}.{fraction}" if fraction else whole


def embedded_score(root: ET.Element) -> str | None:
    path = "CommunityRating" if root.tag == "ComicInfo" else "CommunityRating/AverageRating"
    raw = root.findtext(path)
    if raw is None:
        return None
    if len(raw) > 128:
        return "(score too long to select)"
    return shared_score(raw) or (raw.strip() or "(empty score)")


def annotated_rating(mi: ET.Element) -> bool:
    node = mi.find("CommunityRating")
    return node is not None and bool(
        node.attrib
        or (node.text or "").strip()
        or any(
            child.tag not in {"AverageRating", "RatingCount"}
            or child.attrib
            or len(child)
            or (child.tail or "").strip()
            for child in node
        )
    )
