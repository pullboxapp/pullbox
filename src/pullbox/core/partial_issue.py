"""Explicit story extracts are not evidence of complete issue coverage."""

import re

_STORY_EXTRACT = re.compile(
    r"(?<![\w.])[+-]?\d+(?:\.\d+)?(?:-?[A-Za-z]+)?"
    r"(?:[\s._]+|\s*[\[(]\s*)[A-Za-z][\s._]+Story(?=$|[\s._\])\-])",
    re.IGNORECASE,
)


def is_story_extract(value: str | None) -> bool:
    """Require an issue designation before the marker; never guess from page count."""
    return bool(value and _STORY_EXTRACT.search(value))
