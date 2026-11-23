"""A Notes correction changes prose, never the original document's identity evidence."""

import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from pullbox.core.comicinfo_sanitizer import references_stale_retailer

NOTES_LIMIT = 4096
_TOKEN = re.compile(
    r"\[(?:(?:cv_vol_id|cv_issue_id|cvid):|cvdb)[0-9]+\]"
    r"|\b(?:cvid|issueid)[: \t]+[0-9]+\b",
    re.I,
)
_RESERVED = re.compile(r"cv_vol_id|cv_issue_id|cvid|cvdb|issueid|comicvine|40[05]0-", re.I)


class UneditableNotesError(ValueError):
    """The requested change would lose or reinterpret protected content."""


def safe_notes_prose(value: str) -> bool:
    return (
        len(value) <= NOTES_LIMIT
        and not _RESERVED.search(value)
        and not references_stale_retailer(value)
        and all(c in "\n\t" or c.isprintable() for c in value)
    )


@dataclass(frozen=True)
class IssueNotes:
    prose: str | None
    suffix: str

    def replace(self, prose: str | None) -> str | None:
        result = " ".join(part for part in (prose, self.suffix) if part)
        if len(result) > NOTES_LIMIT:
            raise UneditableNotesError(
                "Shorten Notes to leave room for the file's protected identity markers."
            )
        return result or None


def document_notes(root: ET.Element) -> IssueNotes:
    node = root.find("Notes")
    if node is None:
        return IssueNotes(None, "")
    raw = node.text or ""
    if len(raw) <= NOTES_LIMIT and not node.attrib and not len(node):
        token = _TOKEN.search(raw)
        prose = raw[: token.start()] if token else raw
        suffix = raw[len(prose) :].strip()
        # Only separate a contiguous trailing group. Never rearrange ambiguous inline evidence.
        if (
            (not suffix or not prose or prose[-1].isspace())
            and not _TOKEN.sub("", suffix).strip()
            and safe_notes_prose(prose.strip())
        ):
            return IssueNotes(prose.strip() or None, suffix)
    raise UneditableNotesError(
        "Notes are read-only because Pullbox cannot safely separate prose from possible "
        "identity evidence or unsupported content. Leave Notes unchanged or correct the source."
    )
