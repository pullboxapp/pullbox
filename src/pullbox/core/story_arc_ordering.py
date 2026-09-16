"""Conservative filename evidence for story-arc reading order."""

from __future__ import annotations

import re
from dataclasses import dataclass

_ORDER_PREFIX = re.compile(r"^(?P<order>\d{1,9})\s*-\s*(?P<residual>\S(?:.*\S)?)$")


@dataclass(frozen=True, slots=True)
class StoryArcOrderPrefix:
    """One anchored order prefix without any inferred series identity."""

    reading_order: int
    reading_order_raw: str
    residual_file_name: str

    @property
    def sort_key(self) -> tuple[int, str, str, str]:
        """Return stable numeric-first ordering for review evidence."""
        return (
            self.reading_order,
            self.reading_order_raw,
            self.residual_file_name.casefold(),
            self.residual_file_name,
        )


def extract_story_arc_order_prefix(file_name: str) -> StoryArcOrderPrefix | None:
    """Extract a leading ``NNN-`` order as weak evidence only.

    This deliberately does not parse the residual text as a series or issue.
    Callers must keep an unconfirmed prefix reviewable rather than rewriting
    canonical identity from it.
    """
    if not file_name or len(file_name) > 500 or "/" in file_name or "\\" in file_name:
        return None
    match = _ORDER_PREFIX.fullmatch(file_name.strip())
    if match is None:
        return None
    reading_order = int(match.group("order"))
    if reading_order <= 0:
        return None
    return StoryArcOrderPrefix(
        reading_order=reading_order,
        reading_order_raw=match.group("order"),
        residual_file_name=match.group("residual"),
    )
