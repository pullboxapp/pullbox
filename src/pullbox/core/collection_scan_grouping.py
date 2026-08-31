"""Grouping and scan-candidate helpers for collection imports."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from pullbox.core.type_semantics import TypeFamily, issue_type_family
from pullbox.models.issue import IssueType

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Protocol

    class DiscoveredFileLike(Protocol):
        file_name: str
        parsed_series: str | None
        parsed_issue_number: float | None
        comicvine_issue_id: int | None
        comicvine_series_id: int | None
        has_comicinfo: bool
        issue_type: IssueType
        metadata_signals: dict[str, str]


_LOW_SIGNAL_GROUPING_TOKENS: frozenset[str] = frozenset(
    {
        "scan",
        "scans",
        "dup",
        "dupe",
        "duplicate",
        "copy",
        "digital",
        "fixed",
    }
)

_GENERIC_FILE_SERIES_RE = re.compile(
    r"^(?:issue|issues|chapter|chapters|part|parts|book|books|file|files)\b(?:\s+\d+.*)?$",
    re.IGNORECASE,
)

_GENERIC_SERIES_CONTAINER_IDENTITIES: frozenset[str] = frozenset(
    {
        "books",
        "collection",
        "comics",
        "downloads",
        "imports",
        "incoming",
        "library",
        "media",
        "raw imports",
        "staging",
    }
)

_EXACT_SERIES_METADATA_SIGNALS: frozenset[str] = frozenset(
    {"comicinfo", "mylar3", "pullbox_folder", "sidecar", "source_layout"}
)

_LEADING_ISSUE_TITLE_RE = re.compile(
    r"^(?:issue|issues)\s*#?\s*\d+(?:\.\d+)?\b",
    re.IGNORECASE,
)

_EXPLICIT_ISSUE_MARKER_RE = re.compile(
    r"\bissue\s*#?\s*\d+(?:\.\d+)?\b",
    re.IGNORECASE,
)

_OBVIOUS_NON_COMIC_PDF_RE = re.compile(
    r"\b(?:"
    r"booking\s+confirmation|"
    r"invoice|"
    r"payment(?:\s+(?:confirmation|portal|receipt))?|"
    r"receipt|"
    r"resume|"
    r"statement|"
    r"boarding\s+pass|"
    r"itinerary|"
    r"tax(?:\s+document)?|"
    r"w\s*2|"
    r"1099"
    r")\b",
    re.IGNORECASE,
)


def _normalize_series_identity(value: str) -> str:
    """Normalize a parsed series string for grouping comparisons."""
    normalized = re.sub(r"\s+", " ", value.strip().lower())
    return re.sub(r"[^a-z0-9 ]+", "", normalized)


def _strip_year_volume_tokens(value: str) -> str:
    """Remove Mylar-style inline year markers like ``v2016`` for label comparison."""
    return re.sub(r"\bv\d{4}\b", "", value, flags=re.IGNORECASE).strip()


def _series_identity_tokens(value: str) -> set[str]:
    """Tokenize a series label for grouping comparisons."""
    normalized = _normalize_series_identity(_strip_year_volume_tokens(value))
    return {token for token in normalized.split() if token and not token.isdigit()}


def _should_collapse_to_folder_identity(parsed_series: str, folder_name: str) -> bool:
    """Return true when a parsed series only differs from the folder by release noise."""
    parsed_tokens = _series_identity_tokens(parsed_series)
    folder_tokens = _series_identity_tokens(folder_name)
    if not parsed_tokens or not folder_tokens:
        return False
    if parsed_tokens == folder_tokens:
        return True
    if not folder_tokens.issubset(parsed_tokens):
        return False
    extras = parsed_tokens - folder_tokens
    return bool(extras) and extras.issubset(_LOW_SIGNAL_GROUPING_TOKENS)


def _series_identity_token_sequence(value: str) -> tuple[str, ...]:
    """Return ordered, non-numeric identity tokens for prefix comparisons."""
    normalized = _normalize_series_identity(_strip_year_volume_tokens(value))
    return tuple(token for token in normalized.split() if token and not token.isdigit())


def _should_use_folder_identity_for_issue_title_file(
    discovered_file: DiscoveredFileLike,
    folder_name: str,
) -> bool:
    """Recognize title-bearing issue filenames inside a real series folder."""
    if (
        discovered_file.parsed_issue_number is None
        or not discovered_file.parsed_series
        or discovered_file.comicvine_series_id is not None
        or discovered_file.metadata_signals.get("series_name") in _EXACT_SERIES_METADATA_SIGNALS
    ):
        return False

    normalized_folder = _normalize_series_identity(folder_name)
    if not normalized_folder or normalized_folder in _GENERIC_SERIES_CONTAINER_IDENTITIES:
        return False

    file_stem = re.sub(r"[._-]+", " ", Path(discovered_file.file_name).stem)
    file_stem = re.sub(r"\s+", " ", file_stem).strip()
    if _LEADING_ISSUE_TITLE_RE.match(file_stem):
        return True
    if not _EXPLICIT_ISSUE_MARKER_RE.search(file_stem):
        return False

    folder_tokens = _series_identity_token_sequence(folder_name)
    file_tokens = _series_identity_token_sequence(file_stem)
    return bool(folder_tokens) and file_tokens[: len(folder_tokens)] == folder_tokens


def _is_low_signal_file_series_name(parsed_series: str) -> bool:
    """Return true when a parsed filename series looks like a generic placeholder."""
    normalized = re.sub(r"[_-]+", " ", parsed_series.strip())
    normalized = re.sub(r"\s+", " ", normalized)
    return bool(_GENERIC_FILE_SERIES_RE.match(normalized))


def _has_strong_file_identity(discovered_file: DiscoveredFileLike) -> bool:
    """Return true when a parsed file has enough identity to stand on its own."""
    return any(
        (
            discovered_file.parsed_issue_number is not None,
            discovered_file.comicvine_issue_id is not None,
            discovered_file.comicvine_series_id is not None,
            discovered_file.has_comicinfo,
            discovered_file.metadata_signals.get("series_name") == "source_layout",
        )
    )


def _non_standard_group_discriminator(issue_type: IssueType) -> str | None:
    """Return a grouping discriminator for releases that should not merge with issues."""
    if issue_type_family(issue_type) in {TypeFamily.ISSUE_LIKE, TypeFamily.COLLECTION}:
        return issue_type.value
    return None


def _type_qualified_issue_like_label(series_name: str, issue_type: IssueType) -> str:
    """Return a review label without turning metadata-only markers into title text."""
    if issue_type == IssueType.ONE_SHOT:
        return series_name

    type_label = issue_type.display_name.strip()
    if not type_label or issue_type_family(issue_type) != TypeFamily.ISSUE_LIKE:
        return series_name

    series_tokens = set(_normalize_series_identity(series_name).split())
    label_tokens = set(_normalize_series_identity(type_label).split())
    if label_tokens and label_tokens.issubset(series_tokens):
        return series_name
    return f"{series_name} {type_label}".strip()


def _is_obvious_non_comic_document(path: Path) -> bool:
    """Return true for document-like PDFs that should not enter import review."""
    if path.suffix.lower() != ".pdf":
        return False
    normalized = re.sub(r"[_\-.]+", " ", path.stem)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return bool(_OBVIOUS_NON_COMIC_PDF_RE.search(normalized))


def _is_scan_candidate_file(path: Path, extensions: frozenset[str]) -> bool:
    """Return true when a path is a supported import candidate."""
    if path.name.startswith("._"):
        return False
    return path.suffix.lower() in extensions and not _is_obvious_non_comic_document(path)


def _source_issue_type_for_group(files: Sequence[DiscoveredFileLike]) -> IssueType | None:
    """Return a safe group-level issue type without letting mixed rows over-specialize."""
    issue_types = {discovered_file.issue_type for discovered_file in files}
    if len(issue_types) == 1:
        return next(iter(issue_types))
    if IssueType.ISSUE in issue_types:
        return IssueType.ISSUE
    return None
