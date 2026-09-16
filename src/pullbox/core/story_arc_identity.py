"""Conservative story-arc identity normalization."""

from __future__ import annotations

import re
import unicodedata

_WHITESPACE = re.compile(r"\s+")


def normalize_story_arc_name(name: str) -> str:
    """Return a stable duplicate-detection key without fuzzy title rewriting.

    Story-arc names are user-visible collection identities. Canonical Unicode
    normalization, case folding, and whitespace collapse are safe identity
    operations; punctuation and articles remain significant.
    """
    normalized = unicodedata.normalize("NFC", name)
    normalized = _WHITESPACE.sub(" ", normalized).strip().casefold()
    if not normalized:
        msg = "Story-arc name must not be blank"
        raise ValueError(msg)
    return normalized
