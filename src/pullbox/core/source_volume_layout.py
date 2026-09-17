"""Evidence-aware source folder hints; never a managed destination layout."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath

from pullbox.core.library_layout import split_series_year
from pullbox.core.source_metadata import SourceMetadataExtractor

_MARKER = re.compile(r"v([0-9]{1,4})", re.IGNORECASE)
_GENERIC = frozenset({"comics", "collection", "books", "downloads", "imports", "media", "library"})


def normalized_title(value: str) -> str:
    """Compare names without discarding non-ASCII letters or numeric titles."""
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


@dataclass(frozen=True, slots=True)
class VolumeLeaf:
    series: str
    publisher: str | None
    marker: str
    year: int | None
    volume: int | None
    parent_year_conflict: bool = False

    def evidence(self, *, confirmed: bool, review_required: bool) -> dict[str, object]:
        return {
            "series": self.series,
            "publisher": self.publisher,
            "marker": self.marker,
            "series_year_hint": self.year,
            "volume_hint": self.volume,
            "confirmed": confirmed,
            "review_required": review_required,
        }


def volume_leaf_from_path(relative_path: str) -> VolumeLeaf | None:
    """Recognize only supported root-relative shapes without traversing ancestors."""
    path = PurePosixPath(relative_path.replace("\\", "/"))
    parts = path.parts
    if path.is_absolute() or ".." in parts or len(parts) not in (3, 4):
        return None
    marker = _MARKER.fullmatch(parts[-2])
    if marker is None or int(marker[1]) < 1:
        return None
    parent = re.sub(r"\s+\[(?:cv-)?\d+\]$", "", parts[-3], flags=re.IGNORECASE)
    series, parent_year = split_series_year(parent)
    if not series or normalized_title(series) in _GENERIC:
        return None
    value = int(marker[1])
    year = value if len(marker[1]) == 4 and 1800 <= value <= 2099 else None
    publisher = parts[0] if len(parts) == 4 and parts[0].casefold() not in _GENERIC else None
    return VolumeLeaf(
        series=series,
        publisher=publisher,
        marker=parts[-2],
        year=year or parent_year,
        volume=value if year is None else None,
        parent_year_conflict=year is not None and parent_year is not None and year != parent_year,
    )


def assess_volume_leaf(
    leaf: VolumeLeaf,
    file_name: str,
    *,
    metadata_names: tuple[str, ...] = (),
    proven_file_identity: bool = False,
    identity_conflict: bool = False,
    metadata_series_year: int | None = None,
) -> tuple[bool, bool, bool]:
    """Return (literal series title, corroborated layout, needs identity review).

    Exact per-file identity survives a wrong folder. A sidecar alone cannot
    silently overrule a conflicting filename or another metadata identity.
    """
    parsed = SourceMetadataExtractor().from_release_title(file_name)
    if re.match(rf"^{re.escape(leaf.marker)}[ ._-]+#?\d", file_name, re.IGNORECASE):
        return True, False, False
    filename_title = parsed.series_name if parsed.issue_number is not None else None
    names = [name for name in (*metadata_names, filename_title) if name]
    if any(normalized_title(name) == normalized_title(leaf.marker) for name in names):
        return True, False, False
    expected = normalized_title(leaf.series)
    confirmed = bool(names) and all(normalized_title(name) == expected for name in names)
    if identity_conflict:
        return False, False, True
    if leaf.parent_year_conflict or (
        metadata_series_year is not None
        and leaf.year is not None
        and metadata_series_year != leaf.year
    ):
        return False, False, not proven_file_identity
    if confirmed:
        return False, True, False
    return False, False, not proven_file_identity
