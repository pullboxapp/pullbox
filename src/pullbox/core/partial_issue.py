"""Explicit story extracts are not evidence of complete issue coverage."""

import re
from collections.abc import Mapping

INCOMPLETE_RECOVERY_DISMISSED = "Recovery dismissed; the incomplete file was left unchanged."
METADATA_RECOVERY_SUPERSEDED = (
    "Superseded by a verified library copy; the previous transfer was not marked successful."
)
QUIET_METADATA_RECOVERY_MESSAGES = frozenset(
    {INCOMPLETE_RECOVERY_DISMISSED, METADATA_RECOVERY_SUPERSEDED}
)

_STORY_EXTRACT = re.compile(
    r"(?<![\w.])[+-]?\d+(?:\.\d+)?(?:-?[A-Za-z]+)?"
    r"(?:[\s._]+|\s*[\[(]\s*)[A-Za-z][\s._]+Story(?=$|[\s._\])\-])",
    re.IGNORECASE,
)


def is_story_extract(value: str | None) -> bool:
    """Require an issue designation before the marker; never guess from page count."""
    return bool(value and _STORY_EXTRACT.search(value))


def needs_complete_copy(details: Mapping[str, object], release_title: str | None = None) -> bool:
    """Classify only an explicit partial-file recovery, not arbitrary metadata errors."""
    return (
        details.get("source_kind") == "direct"
        and details.get("failure_code") == "direct_metadata_review_required"
        and (
            details.get("metadata_failure_code") == "partial_issue"
            or is_story_extract(release_title)
        )
    )
