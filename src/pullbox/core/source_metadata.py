"""Shared source metadata extraction for search, library matching, and import."""

from __future__ import annotations

import enum
import json
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast

from pullbox.core.archive import ArchiveError, ArchiveReader
from pullbox.core.naming import detect_issue_type
from pullbox.core.release_parser import ParsedRelease, normalize_issue_number, parse_release_title
from pullbox.core.type_semantics import TypeFamily, issue_type_family
from pullbox.models.issue import IssueType

if TYPE_CHECKING:
    from pullbox.core.comicinfo import ComicInfoData

_CV_ISSUE_URL_RE = re.compile(r"comicvine\.gamespot\.com/.*?/4000-(\d+)", re.IGNORECASE)
_CV_SERIES_URL_RE = re.compile(r"comicvine\.gamespot\.com/.*?/4050-(\d+)", re.IGNORECASE)
_CV_ANY_ID_RE = re.compile(
    r"\b(?:comicid|comicvineid|cv_vol_id|cvid|issueid)\s*[:=]\s*(\d+)\b",
    re.I,
)
_CV_NOTES_PATTERNS = (
    re.compile(r"\[cv_vol_id:(\d+)\]", re.IGNORECASE),
    re.compile(r"\[cvid:(\d+)\]", re.IGNORECASE),
    re.compile(r"\bCVID[:\s]+(\d+)\b", re.IGNORECASE),
)
_CV_ISSUE_NOTES_PATTERNS = (
    re.compile(r"\[cv_issue_id:(\d+)\]", re.IGNORECASE),
    re.compile(r"\bissueid[:\s]+(\d+)\b", re.IGNORECASE),
)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_ORDINAL_SUFFIX_WORDS: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
_SERIES_ORDINAL_SUFFIX_RE = re.compile(
    r"^(?P<base>.+?)\s+(?:book|part|volume|vol\.?)\s+"
    rf"(?P<number>\d+(?:\.\d+)?|{'|'.join(_ORDINAL_SUFFIX_WORDS)})$",
    re.IGNORECASE,
)
_SERIES_SUBTITLE_RE = re.compile(r"^(?P<base>.+?)\s*[-:]\s*(?P<subtitle>.+)$")
_ARCHIVE_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".tif", ".tiff"})
_ARCHIVE_PAGE_SUFFIX_RE = re.compile(
    r"(?:[\s._-](?:p(?:age)?\s*)?0*\d{1,4})$",
    re.IGNORECASE,
)
_ARCHIVE_HINT_MIN_PARSEABLE_IMAGES = 3
_ARCHIVE_HINT_MIN_PARSEABLE_CONSENSUS = 0.80
_ARCHIVE_HINT_MIN_TOTAL_CONSENSUS = 0.60
_VOLUME_HINT_PUBLISHER_PREFIX_RE = re.compile(
    r"^(?:DC(?:\s+Comics)?|Marvel(?:\s+Comics)?|Image(?:\s+Comics)?|"
    r"Dark\s+Horse(?:\s+Comics)?|IDW(?:\s+Publishing)?|"
    r"BOOM!?(?:\s+Studios)?|Dynamite(?:\s+Entertainment)?|"
    r"Oni(?:\s+Press)?|Titan(?:\s+Comics)?|Yen(?:\s+Press)?|"
    r"Archie|Fantagraphics|AWA(?:\s+Studios)?|Tidalwave(?:\s+Productions)?|"
    r"Z2(?:\s+Comics)?|Noir(?:\s+Caesar)?|Disney|Humanoids|"
    r"Valiant|Viz(?:\s+Media)?|Kodansha(?:\s+Comics)?|AfterShock(?:\s+Comics)?|"
    r"Mad\s+Cave(?:\s+Studios)?|Ablaze|Vault(?:\s+Comics)?|DSTLRY|"
    r"Ahoy(?:\s+Comics)?|Scout(?:\s+Comics)?|TKO(?:\s+Studios)?|"
    r"Antarctic(?:\s+Press)?|Rebellion|Top\s+Cow|Lion\s+Forge|Zenescope|"
    r"Avatar(?:\s+Press)?|Action\s+Lab|Aspen(?:\s+Comics)?|Vertigo|"
    r"Black\s+Mask|Magnetic\s+Press|Papercutz)\s*-\s*",
    re.IGNORECASE,
)


class VolumeSubtitleHint(TypedDict):
    """Structured filename hint for collected-volume issue matching."""

    base_series: str
    subtitle: str
    issue_number: float


class ArchiveEntryIssueHint(TypedDict):
    """Strong issue-number signal derived from archive page filenames."""

    series_name: str
    issue_number: float
    year: int | None
    confidence: str
    total_image_entries: int
    parseable_image_entries: int
    matching_entry_count: int
    sample_entries: list[str]


def _volume_issue_number_from_text(volume: str | None) -> float | None:
    if not volume:
        return None
    normalized = normalize_issue_number(volume)
    if normalized is not None:
        return normalized
    match = re.search(r"(\d+(?:\.\d+)?)", volume)
    if match:
        return normalize_issue_number(match.group(1))
    return None


def _clean_volume_subtitle_hint(raw_subtitle: str | None) -> str | None:
    """Return real title text after a volume token, ignoring release metadata."""
    subtitle = re.sub(r"\s+", " ", raw_subtitle or "").strip(" -:")
    if not subtitle:
        return None
    if subtitle[0] in "([":
        return None

    subtitle = re.split(r"\s+[\(\[]", subtitle, maxsplit=1)[0].strip()
    subtitle = re.split(
        r"\s+(?:19|20)\d{2}(?:\s+(?:Retail|Comic|eBook|Digital|Hybrid|Webrip|c2c|HD)\b|$)",
        subtitle,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip()
    subtitle = re.sub(r"\s+(?:19|20)\d{2}$", "", subtitle).strip()
    if not subtitle or re.fullmatch(r"(?:19|20)\d{2}", subtitle):
        return None
    if not re.search(r"[A-Za-z]", subtitle):
        return None
    return subtitle


def volume_subtitle_hint_from_filename(
    file_name: str,
    *,
    volume: str | None = None,
    issue_number: float | None = None,
) -> VolumeSubtitleHint | None:
    """Return base-series and subtitle hints from ``Series Vol NN Subtitle`` names."""
    if not volume:
        parsed = parse_release_title(file_name)
        volume = parsed.volume if parsed is not None else None
    issue_number = issue_number or _volume_issue_number_from_text(volume)
    if not volume or issue_number is None:
        return None

    normalized_stem = Path(file_name).name
    normalized_stem = re.sub(
        r"\.(?:cbz|cbr|cb7|pdf|epub)$",
        "",
        normalized_stem,
        flags=re.IGNORECASE,
    )
    normalized_stem = re.sub(r"[_\.]+", " ", normalized_stem)
    normalized_stem = re.sub(r"\s+", " ", normalized_stem).strip()
    volume_number = re.search(r"(\d+(?:\.\d+)?)", volume)
    if not volume_number:
        return None
    volume_token = re.escape(volume_number.group(1).lstrip("0") or "0")
    volume_pattern = re.compile(
        rf"\b(?:v|vol\.?|volume)\s*0*{volume_token}\b",
        re.IGNORECASE,
    )
    match = volume_pattern.search(normalized_stem)
    if not match:
        return None
    base_series = re.sub(r"\s+", " ", normalized_stem[: match.start()].strip(" -:"))
    base_series = re.sub(
        r"\b(?:TPB|Trade\s+Paperback|Paperback|Hardcover|HC|Omnibus|"
        r"Compendium|Deluxe(?:\s+Edition)?|Graphic\s+Novel|GN|OGN)\b",
        "",
        base_series,
        flags=re.IGNORECASE,
    )
    base_series = re.sub(r"\s+", " ", base_series).strip(" -:")
    base_series = _VOLUME_HINT_PUBLISHER_PREFIX_RE.sub("", base_series).strip(" -:")
    subtitle = _clean_volume_subtitle_hint(normalized_stem[match.end() :])
    if not base_series or not subtitle:
        return None
    return {
        "base_series": base_series,
        "subtitle": subtitle,
        "issue_number": issue_number,
    }


def archive_entry_issue_hint_from_names(
    entry_names: list[str],
    *,
    expected_series_name: str | None = None,
) -> ArchiveEntryIssueHint | None:
    """Return a strong issue hint from image entry names without reading image data."""
    image_entries = [
        entry
        for entry in entry_names
        if Path(entry.replace("\\", "/").rsplit("/", 1)[-1]).suffix.lower()
        in _ARCHIVE_IMAGE_SUFFIXES
    ]
    if len(image_entries) < _ARCHIVE_HINT_MIN_PARSEABLE_IMAGES:
        return None

    parsed_entries: list[tuple[str, float, int | None, str]] = []
    for entry in image_entries:
        file_name = entry.replace("\\", "/").rsplit("/", 1)[-1]
        stem = Path(file_name).stem
        candidate_title = _ARCHIVE_PAGE_SUFFIX_RE.sub("", stem).strip()
        if not candidate_title or candidate_title == stem:
            continue
        parsed = parse_release_title(candidate_title)
        if parsed is None or parsed.issue_number is None or not parsed.series_name:
            continue
        if expected_series_name and not _archive_entry_series_matches(
            parsed.series_name,
            expected_series_name,
        ):
            continue
        parsed_entries.append((parsed.series_name, float(parsed.issue_number), parsed.year, entry))

    total_image_entries = len(image_entries)
    parseable_image_entries = len(parsed_entries)
    if parseable_image_entries < _ARCHIVE_HINT_MIN_PARSEABLE_IMAGES:
        return None

    issue_counts = Counter(issue_number for _series, issue_number, _year, _entry in parsed_entries)
    ranked_issues = issue_counts.most_common(2)
    winning_issue, winning_count = ranked_issues[0]
    if len(ranked_issues) > 1 and ranked_issues[1][1] == winning_count:
        return None
    if winning_count / parseable_image_entries < _ARCHIVE_HINT_MIN_PARSEABLE_CONSENSUS:
        return None
    if (
        total_image_entries
        and winning_count / total_image_entries < _ARCHIVE_HINT_MIN_TOTAL_CONSENSUS
    ):
        return None

    matching_entries = [
        entry
        for _series, issue_number, _year, entry in parsed_entries
        if _issue_numbers_equal(issue_number, winning_issue)
    ]
    series_counts = Counter(
        series
        for series, issue_number, _year, _entry in parsed_entries
        if _issue_numbers_equal(issue_number, winning_issue)
    )
    year_counts = Counter(
        year
        for _series, issue_number, year, _entry in parsed_entries
        if _issue_numbers_equal(issue_number, winning_issue) and year is not None
    )
    return {
        "series_name": series_counts.most_common(1)[0][0],
        "issue_number": winning_issue,
        "year": year_counts.most_common(1)[0][0] if year_counts else None,
        "confidence": "strong",
        "total_image_entries": total_image_entries,
        "parseable_image_entries": parseable_image_entries,
        "matching_entry_count": winning_count,
        "sample_entries": matching_entries[:3],
    }


def _archive_entry_series_matches(candidate: str, expected: str) -> bool:
    candidate_key = _series_identity_key(candidate, drop_leading_article=True)
    expected_key = _series_identity_key(expected, drop_leading_article=True)
    return bool(candidate_key and expected_key and candidate_key == expected_key)


class MetadataSignal(enum.StrEnum):
    """Which input source supplied a resolved metadata field."""

    RELEASE_TITLE = "release_title"
    COMICINFO = "comicinfo"
    SIDECAR = "sidecar"
    FOLDER_HINT = "folder_hint"
    MYLAR3 = "mylar3"


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    """Canonical extracted metadata for one source candidate."""

    original_title: str
    source_path: str | None = None
    series_name: str | None = None
    issue_number: float | None = None
    year: int | None = None
    volume: str | None = None
    issue_type: IssueType = IssueType.ISSUE
    publisher: str | None = None
    comicvine_series_id: int | None = None
    comicvine_issue_id: int | None = None
    series_status: str | None = None
    issue_count_hint: int | None = None
    folder_issue_type: IssueType | None = None
    parsed_release: ParsedRelease | None = None
    signals: dict[str, MetadataSignal] = field(default_factory=dict)
    diagnostics: dict[str, object] = field(default_factory=dict)

    def model_copy(self, *, update: dict[str, object] | None = None) -> SourceMetadata:
        """Pydantic-like convenience used in tests and internal refactors."""
        return replace(self, **cast("dict[str, Any]", update or {}))


class SourceMetadataExtractor:
    """Extract canonical metadata from release titles and archive files."""

    def from_path(
        self,
        archive_path: str | Path,
        *,
        include_archive_comicinfo: bool = True,
        include_archive_entry_issue_hint: bool = True,
        sidecar_data: dict[str, Any] | None = None,
    ) -> SourceMetadata:
        """Build metadata from filename, folder sidecars, and optional archive ComicInfo."""
        path = Path(archive_path)
        folder_name = path.parent.name
        metadata = self.from_release_title(
            path.name,
            source_path=str(path),
            folder_name=folder_name,
        )
        comicinfo = self._read_archive_comicinfo(path) if include_archive_comicinfo else None
        archive_entry_issue_hint = (
            self.archive_entry_issue_hint_from_path(
                path,
                expected_series_name=metadata.series_name,
            )
            if (
                include_archive_comicinfo and include_archive_entry_issue_hint and comicinfo is None
            )
            else None
        )
        sidecar = sidecar_data if sidecar_data is not None else self._read_sidecars(path.parent)

        return self._merge_path_metadata(
            metadata=metadata,
            folder_name=folder_name,
            comicinfo=comicinfo,
            archive_entry_issue_hint=archive_entry_issue_hint,
            sidecar=sidecar,
            include_archive_comicinfo=include_archive_comicinfo,
            include_archive_entry_issue_hint=include_archive_entry_issue_hint,
        )

    def from_release_title(
        self,
        title: str,
        *,
        source_path: str | None = None,
        folder_name: str | None = None,
    ) -> SourceMetadata:
        parsed = parse_release_title(title)
        folder_issue_type = self._folder_issue_type(folder_name)
        issue_type = IssueType.ISSUE
        if parsed is not None:
            issue_type = parsed.issue_type
        if folder_issue_type is not None and (issue_type == IssueType.ISSUE or parsed is None):
            issue_type = folder_issue_type
        signals: dict[str, MetadataSignal] = {}
        if parsed and parsed.issue_type != IssueType.ISSUE:
            signals["issue_type"] = MetadataSignal.RELEASE_TITLE
        elif folder_issue_type is not None:
            signals["issue_type"] = MetadataSignal.FOLDER_HINT

        series_name = parsed.series_name if parsed is not None else None
        issue_number = parsed.issue_number if parsed is not None else None
        year = parsed.year if parsed is not None else None
        volume = parsed.volume if parsed is not None else None
        diagnostics: dict[str, object] = {}

        volume_issue_number = _volume_issue_number_from_text(volume)
        volume_hint = volume_subtitle_hint_from_filename(
            title,
            volume=volume,
            issue_number=volume_issue_number,
        )
        volume_issue_applies = issue_type_family(issue_type) == TypeFamily.COLLECTION
        if volume_issue_applies and volume_hint is not None and issue_number is None:
            series_name = str(volume_hint["base_series"])
            issue_number = volume_hint["issue_number"]
            signals["issue_number"] = MetadataSignal.RELEASE_TITLE
            diagnostics["volume_subtitle_hint"] = volume_hint
        elif volume_issue_applies and issue_number is None and volume_issue_number is not None:
            issue_number = volume_issue_number
            signals["issue_number"] = MetadataSignal.RELEASE_TITLE

        return SourceMetadata(
            original_title=title,
            source_path=source_path,
            series_name=series_name,
            issue_number=issue_number,
            year=year,
            volume=volume,
            issue_type=issue_type,
            folder_issue_type=folder_issue_type,
            parsed_release=parsed,
            signals=signals,
            diagnostics=diagnostics,
        )

    def from_archive_path(
        self,
        archive_path: str | Path,
        *,
        include_archive_comicinfo: bool = True,
        include_archive_entry_issue_hint: bool = True,
    ) -> SourceMetadata:
        return self.from_path(
            archive_path,
            include_archive_comicinfo=include_archive_comicinfo,
            include_archive_entry_issue_hint=include_archive_entry_issue_hint,
        )

    def read_sidecars(self, folder: str | Path) -> dict[str, Any]:
        """Return normalized sidecar metadata for a folder."""
        return self._read_sidecars(Path(folder))

    def _merge_path_metadata(
        self,
        *,
        metadata: SourceMetadata,
        folder_name: str,
        comicinfo: ComicInfoData | None,
        archive_entry_issue_hint: ArchiveEntryIssueHint | None,
        sidecar: dict[str, Any],
        include_archive_comicinfo: bool,
        include_archive_entry_issue_hint: bool,
    ) -> SourceMetadata:
        series_name = metadata.series_name
        issue_number = metadata.issue_number
        year = metadata.year
        volume = metadata.volume
        publisher = metadata.publisher
        comicvine_series_id = None
        comicvine_issue_id = None
        issue_type = metadata.issue_type
        series_status = None
        issue_count_hint = None
        signals = dict(metadata.signals)
        identity_conflicts: list[dict[str, object]] = []
        diagnostics: dict[str, object] = {
            "folder_name": folder_name,
            "sidecar_files_present": sorted(sidecar["files_present"]),
            "filename_parse": _serialize_parsed_release(metadata.parsed_release),
            "archive_metadata_loaded": include_archive_comicinfo,
            "archive_metadata_deferred": not include_archive_comicinfo,
        }
        diagnostics.update(metadata.diagnostics)

        if comicinfo:
            if comicinfo.series:
                ignored_ordinal_suffix = _comicinfo_series_redundant_ordinal_suffix(
                    comicinfo_series=comicinfo.series,
                    filename_series=metadata.series_name,
                    filename_issue_number=metadata.issue_number,
                    comicinfo_issue_number=comicinfo.number,
                )
                ignored_volume_subtitle = _comicinfo_series_redundant_volume_subtitle(
                    comicinfo_series=comicinfo.series,
                    volume_hint=diagnostics.get("volume_subtitle_hint"),
                )
                if ignored_ordinal_suffix is not None:
                    diagnostics["comicinfo_series_ignored"] = ignored_ordinal_suffix
                elif ignored_volume_subtitle is not None:
                    diagnostics["comicinfo_series_ignored"] = ignored_volume_subtitle
                else:
                    series_name = comicinfo.series
                    signals["series_name"] = MetadataSignal.COMICINFO
            if comicinfo.number:
                normalized_comicinfo_issue = normalize_issue_number(comicinfo.number)
                filename_volume_issue_number = _volume_issue_number_from_text(metadata.volume)
                filename_issue_was_volume = (
                    filename_volume_issue_number is not None
                    and metadata.parsed_release is not None
                    and metadata.parsed_release.issue_number is None
                    and metadata.signals.get("issue_number") == MetadataSignal.RELEASE_TITLE
                )
                if (
                    filename_issue_was_volume
                    and normalized_comicinfo_issue is not None
                    and not _issue_numbers_equal(
                        cast("float", filename_volume_issue_number),
                        normalized_comicinfo_issue,
                    )
                ):
                    diagnostics["comicinfo_issue_number_ignored"] = {
                        "reason": "filename_volume_conflict",
                        "number": comicinfo.number,
                        "filename_volume": metadata.volume,
                        "filename_issue_number": filename_volume_issue_number,
                    }
                elif normalized_comicinfo_issue is not None:
                    issue_number = normalized_comicinfo_issue
                    signals["issue_number"] = MetadataSignal.COMICINFO
                else:
                    diagnostics["comicinfo_issue_number_ignored"] = {
                        "reason": "unparseable",
                        "number": comicinfo.number,
                        "filename_issue_number": metadata.issue_number,
                    }
            if comicinfo.volume and comicinfo.volume.isdigit() and len(comicinfo.volume) == 4:
                year = int(comicinfo.volume)
                signals["year"] = MetadataSignal.COMICINFO
            if comicinfo.publisher:
                publisher = comicinfo.publisher
                signals["publisher"] = MetadataSignal.COMICINFO
            comicvine_series_id, comicvine_series_id_source = _extract_series_id_from_web(
                comicinfo.web
            )
            if comicvine_series_id is None:
                comicvine_series_id, comicvine_series_id_source = _extract_series_id_from_notes(
                    comicinfo.notes
                )
            if comicvine_series_id is not None:
                signals["comicvine_series_id"] = MetadataSignal.COMICINFO
                diagnostics["comicvine_series_id_source"] = comicvine_series_id_source
            comicvine_issue_id = _extract_issue_id_from_web(comicinfo.web)
            if comicvine_issue_id is None:
                comicvine_issue_id = _extract_issue_id_from_notes(comicinfo.notes)
            if comicvine_issue_id is not None:
                signals["comicvine_issue_id"] = MetadataSignal.COMICINFO
            diagnostics["has_comicinfo"] = True
            diagnostics["comicinfo"] = {
                "series": comicinfo.series,
                "number": comicinfo.number,
                "volume": comicinfo.volume,
                "year": comicinfo.year,
                "title": comicinfo.title,
                "publisher": comicinfo.publisher,
                "web": comicinfo.web,
                "notes": comicinfo.notes,
            }
        else:
            diagnostics["has_comicinfo"] = False
            if include_archive_comicinfo and include_archive_entry_issue_hint:
                diagnostics["archive_entry_issue_hint_checked"] = True
            if archive_entry_issue_hint is not None:
                diagnostics["archive_entry_issue_hint"] = dict(archive_entry_issue_hint)

        if sidecar["series_id"] is not None:
            if comicvine_series_id is not None and comicvine_series_id != sidecar["series_id"]:
                identity_conflicts.append(
                    {
                        "field": "comicvine_series_id",
                        "comicinfo": comicvine_series_id,
                        "sidecar": sidecar["series_id"],
                    }
                )
            comicvine_series_id = sidecar["series_id"]
            signals["comicvine_series_id"] = MetadataSignal.SIDECAR
        if sidecar["issue_id"] is not None:
            if comicvine_issue_id is not None and comicvine_issue_id != sidecar["issue_id"]:
                identity_conflicts.append(
                    {
                        "field": "comicvine_issue_id",
                        "comicinfo": comicvine_issue_id,
                        "sidecar": sidecar["issue_id"],
                    }
                )
            comicvine_issue_id = sidecar["issue_id"]
            signals["comicvine_issue_id"] = MetadataSignal.SIDECAR
        if identity_conflicts:
            diagnostics["identity_conflicts"] = identity_conflicts
        if sidecar["booktype"] is not None:
            issue_type = sidecar["booktype"]
            signals["issue_type"] = MetadataSignal.SIDECAR
        elif metadata.folder_issue_type is not None and "issue_type" not in signals:
            issue_type = metadata.folder_issue_type
            signals["issue_type"] = MetadataSignal.FOLDER_HINT
        if sidecar["series_status"] is not None:
            series_status = sidecar["series_status"]
        if sidecar["issue_count"] is not None:
            issue_count_hint = sidecar["issue_count"]
        if sidecar["series_name"] is not None and not series_name:
            series_name = sidecar["series_name"]
            signals["series_name"] = MetadataSignal.SIDECAR
        if sidecar["year"] is not None and year is None:
            year = sidecar["year"]
            signals["year"] = MetadataSignal.SIDECAR

        return SourceMetadata(
            original_title=metadata.original_title,
            source_path=metadata.source_path,
            series_name=series_name,
            issue_number=issue_number,
            year=year,
            volume=volume,
            issue_type=issue_type,
            publisher=publisher,
            comicvine_series_id=comicvine_series_id,
            comicvine_issue_id=comicvine_issue_id,
            series_status=series_status,
            issue_count_hint=issue_count_hint,
            folder_issue_type=metadata.folder_issue_type,
            parsed_release=metadata.parsed_release,
            signals=signals,
            diagnostics=diagnostics,
        )

    @staticmethod
    def _folder_issue_type(folder_name: str | None) -> IssueType | None:
        if not folder_name:
            return None
        detected = detect_issue_type(folder_name)
        if detected == "issue":
            return None
        try:
            return IssueType(detected)
        except ValueError:
            return None

    @staticmethod
    def _read_archive_comicinfo(path: Path) -> ComicInfoData | None:
        try:
            return ArchiveReader(path).read_comicinfo()
        except ArchiveError:
            return None

    @staticmethod
    def archive_entry_issue_hint_from_path(
        path: str | Path,
        *,
        expected_series_name: str | None = None,
    ) -> ArchiveEntryIssueHint | None:
        """Inspect archive entry names for a strong internal issue-number consensus."""
        try:
            entries = ArchiveReader(Path(path)).list_files()
        except ArchiveError:
            return None
        if any(entry.lower().endswith("comicinfo.xml") for entry in entries):
            return None
        return archive_entry_issue_hint_from_names(
            entries,
            expected_series_name=expected_series_name,
        )

    def _read_sidecars(self, folder: Path) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "files_present": [],
            "series_id": None,
            "issue_id": None,
            "booktype": None,
            "series_status": None,
            "issue_count": None,
            "series_name": None,
            "year": None,
        }
        for sidecar_name in ("series.json", "cvinfo"):
            path = folder / sidecar_name
            if not path.exists():
                continue
            payload["files_present"].append(sidecar_name)
            raw_text = path.read_text(errors="replace")
            sidecar_data = self._parse_sidecar(raw_text)
            payload["series_id"] = payload["series_id"] or _as_int(
                sidecar_data.get("comicid")
                or sidecar_data.get("comicvine_id")
                or sidecar_data.get("series_id")
            )
            payload["issue_id"] = payload["issue_id"] or _as_int(
                sidecar_data.get("issueid") or sidecar_data.get("comicvine_issue_id")
            )
            payload["booktype"] = payload["booktype"] or _coerce_issue_type(
                sidecar_data.get("booktype")
                or sidecar_data.get("type")
                or sidecar_data.get("series_type")
            )
            payload["series_status"] = payload["series_status"] or _as_str(
                sidecar_data.get("status")
            )
            payload["issue_count"] = payload["issue_count"] or _as_int(
                sidecar_data.get("total_issues")
                or sidecar_data.get("issue_count")
                or sidecar_data.get("issues")
            )
            payload["series_name"] = payload["series_name"] or _as_str(
                sidecar_data.get("name") or sidecar_data.get("series") or sidecar_data.get("title")
            )
            payload["year"] = payload["year"] or _as_year(sidecar_data.get("year"))
        return payload

    @staticmethod
    def _parse_sidecar(raw_text: str) -> dict[str, Any]:
        raw_text = raw_text.strip()
        if not raw_text:
            return {}
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return {str(key).lower(): value for key, value in parsed.items()}
        data: dict[str, Any] = {}
        for line in raw_text.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
            elif "=" in line:
                key, value = line.split("=", 1)
            else:
                continue
            data[key.strip().lower()] = value.strip()
        if not data:
            match = _CV_ANY_ID_RE.search(raw_text)
            if match:
                data["comicid"] = match.group(1)
        return data


def _extract_issue_id_from_web(web: str | None) -> int | None:
    if not web:
        return None
    match = _CV_ISSUE_URL_RE.search(web)
    if match:
        return _as_int(match.group(1))
    return None


def _extract_series_id_from_web(web: str | None) -> tuple[int | None, str | None]:
    if not web:
        return None, None
    url_match = _CV_SERIES_URL_RE.search(web)
    if url_match:
        return _as_int(url_match.group(1)), "comicvine_volume_url"
    return None, None


def _extract_series_id_from_notes(notes: str | None) -> tuple[int | None, str | None]:
    if not notes:
        return None, None
    url_match = _CV_SERIES_URL_RE.search(notes)
    if url_match:
        return _as_int(url_match.group(1)), "comicvine_volume_url"
    for pattern in _CV_NOTES_PATTERNS:
        match = pattern.search(notes)
        if match:
            return _as_int(match.group(1)), "comicvine_note_id"
    return None, None


def _extract_issue_id_from_notes(notes: str | None) -> int | None:
    if not notes:
        return None
    for pattern in _CV_ISSUE_NOTES_PATTERNS:
        match = pattern.search(notes)
        if match:
            return _as_int(match.group(1))
    return None


def _coerce_issue_type(value: Any) -> IssueType | None:
    text = _as_str(value)
    if not text:
        return None
    normalized = text.strip().lower()
    if normalized in {"print", "issue", "standard"}:
        return IssueType.ISSUE
    detection_source = text.replace("_", " ").replace("-", " ")
    detected = detect_issue_type(detection_source)
    try:
        return IssueType(detected)
    except ValueError:
        return None


def _comicinfo_series_redundant_ordinal_suffix(
    *,
    comicinfo_series: str,
    filename_series: str | None,
    filename_issue_number: float | None,
    comicinfo_issue_number: str | None,
) -> dict[str, object] | None:
    """Detect ComicInfo series names that only repeat the filename ordinal."""
    if not filename_series or filename_issue_number is None:
        return None
    match = _SERIES_ORDINAL_SUFFIX_RE.match(comicinfo_series.strip())
    if not match:
        return None
    base_series = match.group("base").strip(" -:")
    if _series_identity_key(base_series) != _series_identity_key(filename_series):
        return None

    suffix_issue_number = _ordinal_suffix_number(match.group("number"))
    if suffix_issue_number is None or not _issue_numbers_equal(
        suffix_issue_number,
        filename_issue_number,
    ):
        return None

    if comicinfo_issue_number:
        normalized_comicinfo_issue = normalize_issue_number(comicinfo_issue_number)
        if normalized_comicinfo_issue is not None and not _issue_numbers_equal(
            suffix_issue_number,
            normalized_comicinfo_issue,
        ):
            return None

    return {
        "reason": "redundant_ordinal_suffix",
        "series": comicinfo_series,
        "filename_series": filename_series,
        "issue_number": suffix_issue_number,
    }


def _comicinfo_series_redundant_volume_subtitle(
    *,
    comicinfo_series: str,
    volume_hint: object,
) -> dict[str, object] | None:
    """Detect ComicInfo series names that repeat a volume subtitle."""
    if not isinstance(volume_hint, dict):
        return None
    filename_series = str(volume_hint.get("base_series") or "").strip()
    filename_subtitle = str(volume_hint.get("subtitle") or "").strip()
    filename_issue_number = volume_hint.get("issue_number")
    if not filename_series or not filename_subtitle or filename_issue_number is None:
        return None

    match = _SERIES_SUBTITLE_RE.match(comicinfo_series.strip())
    if not match:
        return None
    comicinfo_base = match.group("base").strip(" -:")
    comicinfo_subtitle = match.group("subtitle").strip(" -:")
    if _series_identity_key(comicinfo_base, drop_leading_article=True) != _series_identity_key(
        filename_series,
        drop_leading_article=True,
    ):
        return None
    if _series_identity_key(comicinfo_subtitle) != _series_identity_key(filename_subtitle):
        return None

    return {
        "reason": "redundant_volume_subtitle",
        "series": comicinfo_series,
        "filename_series": filename_series,
        "subtitle": filename_subtitle,
        "issue_number": filename_issue_number,
    }


def _ordinal_suffix_number(value: str) -> float | None:
    normalized = value.strip().lower()
    if normalized in _ORDINAL_SUFFIX_WORDS:
        return float(_ORDINAL_SUFFIX_WORDS[normalized])
    return normalize_issue_number(normalized)


def _issue_numbers_equal(left: float, right: float) -> bool:
    return abs(float(left) - float(right)) < 0.0001


def _series_identity_key(value: str, *, drop_leading_article: bool = False) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if drop_leading_article:
        normalized = re.sub(r"^(?:the|a|an)\s+", "", normalized)
    return normalized


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _as_year(value: Any) -> int | None:
    text = _as_str(value)
    if not text:
        return None
    match = _YEAR_RE.search(text)
    if match:
        return int(match.group(0))
    return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _serialize_parsed_release(parsed: ParsedRelease | None) -> dict[str, object]:
    """Serialize raw filename parse data for later diagnostics and UI display."""
    return {
        "series_name": parsed.series_name if parsed is not None else None,
        "issue_number": parsed.issue_number if parsed is not None else None,
        "year": parsed.year if parsed is not None else None,
        "volume": parsed.volume if parsed is not None else None,
        "issue_type": parsed.issue_type.value if parsed is not None else None,
    }
