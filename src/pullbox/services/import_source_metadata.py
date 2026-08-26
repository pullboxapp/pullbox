"""Source metadata helpers for import scan, matching, and repair workflows."""

from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from sqlalchemy import select as sa_select

from pullbox.core.name_matcher import NameMatcher
from pullbox.core.release_parser import normalize_issue_number, parse_release_title
from pullbox.core.source_metadata import (
    MetadataSignal,
    SourceMetadata,
    SourceMetadataExtractor,
    volume_subtitle_hint_from_filename,
)
from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries
from pullbox.models.issue import IssueType

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_matcher = NameMatcher()


class _VolumeSubtitleHint(TypedDict):
    base_series: str
    subtitle: str
    issue_number: float


_TYPE_QUALIFIED_SERIES_HINT_TYPES: frozenset[IssueType] = frozenset(
    {
        IssueType.TPB,
        IssueType.OMNIBUS,
        IssueType.GN,
        IssueType.OGN,
        IssueType.HC,
        IssueType.DELUXE,
        IssueType.COMPENDIUM,
    }
)


def normalized_release_name(file_name: str) -> str:
    """Normalize a filename stem for cheap duplicate-copy detection."""
    return NameMatcher.normalize(Path(file_name).stem)


def _metadata_signals(raw_signals: object) -> dict[str, MetadataSignal]:
    """Restore persisted signal values into the shared metadata enum contract."""
    signals: dict[str, MetadataSignal] = {}
    if isinstance(raw_signals, dict):
        for key, value in raw_signals.items():
            with contextlib.suppress(ValueError):
                signals[str(key)] = MetadataSignal(str(value))
    return signals


def _optional_int(value: object) -> int | None:
    """Return an integer metadata identifier when one was persisted."""
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _has_deferred_archive_metadata(raw_source_metadata: object) -> bool:
    """Return whether scan intentionally deferred archive ComicInfo loading."""
    return bool(
        isinstance(raw_source_metadata, dict)
        and raw_source_metadata.get("archive_metadata_deferred") is True
    )


def _source_issue_type(raw_issue_type: object) -> IssueType:
    """Restore a persisted source issue type with a safe issue default."""
    issue_type = IssueType.ISSUE
    if raw_issue_type:
        with contextlib.suppress(ValueError):
            issue_type = IssueType(str(raw_issue_type))
    return issue_type


def _filename_parse_volume(diagnostics: dict[str, Any], file_name: str | None = None) -> str | None:
    """Return the filename parser volume from persisted diagnostics or filename fallback."""
    source_metadata = diagnostics.get("source_metadata")
    filename_parse = (
        source_metadata.get("filename_parse") if isinstance(source_metadata, dict) else None
    )
    volume = filename_parse.get("volume") if isinstance(filename_parse, dict) else None
    if not volume and file_name:
        parsed = parse_release_title(file_name)
        volume = parsed.volume if parsed is not None else None
    return str(volume) if volume else None


def _filename_parse_issue_number(
    diagnostics: dict[str, Any],
    file_name: str | None = None,
) -> float | None:
    """Return the filename parser issue number from diagnostics or filename fallback."""
    source_metadata = diagnostics.get("source_metadata")
    filename_parse = (
        source_metadata.get("filename_parse") if isinstance(source_metadata, dict) else None
    )
    issue_number = filename_parse.get("issue_number") if isinstance(filename_parse, dict) else None
    normalized = normalize_issue_number(issue_number)
    if normalized is not None:
        return normalized
    if file_name:
        parsed = parse_release_title(file_name)
        return parsed.issue_number if parsed is not None else None
    return None


def _filename_parse_volume_issue_number(
    diagnostics: dict[str, Any],
    file_name: str | None = None,
) -> float | None:
    """Return a volume-derived issue number from persisted diagnostics or filename fallback."""
    volume = _filename_parse_volume(diagnostics, file_name)
    if not volume:
        return None
    normalized = normalize_issue_number(volume)
    if normalized is not None:
        return normalized
    match = re.search(r"(\d+(?:\.\d+)?)", volume)
    if match:
        return normalize_issue_number(match.group(1))
    return None


def _archive_entry_issue_hint(diagnostics: dict[str, Any]) -> dict[str, Any] | None:
    source_metadata = diagnostics.get("source_metadata")
    candidates: list[object] = []
    if isinstance(source_metadata, dict):
        candidates.append(source_metadata.get("archive_entry_issue_hint"))
    candidates.append(diagnostics.get("archive_entry_issue_hint"))
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        issue_number = normalize_issue_number(candidate.get("issue_number"))
        if issue_number is None or candidate.get("confidence") != "strong":
            continue
        return {**candidate, "issue_number": issue_number}
    return None


def _format_issue_number(issue_number: float | None) -> str:
    if issue_number is None:
        return "unknown"
    return f"{issue_number:g}"


def _issue_numbers_equal(left: float, right: float) -> bool:
    return abs(float(left) - float(right)) < 0.0001


def _volume_subtitle_hint(
    file_name: str, diagnostics: dict[str, Any]
) -> _VolumeSubtitleHint | None:
    """Return base-series and subtitle hints from ``Series Vol NN Subtitle`` names."""
    volume = _filename_parse_volume(diagnostics, file_name)
    issue_number = _filename_parse_volume_issue_number(diagnostics, file_name)
    explicit_issue_number = _filename_parse_issue_number(diagnostics, file_name)
    if (
        explicit_issue_number is not None
        and issue_number is not None
        and not _issue_numbers_equal(explicit_issue_number, issue_number)
    ):
        return None
    hint = volume_subtitle_hint_from_filename(
        file_name,
        volume=volume,
        issue_number=issue_number,
    )
    if hint is None:
        return None
    return _VolumeSubtitleHint(
        base_series=hint["base_series"],
        subtitle=hint["subtitle"],
        issue_number=hint["issue_number"],
    )


def _issue_like_series_hints(
    file_name: str,
    release_metadata: SourceMetadata,
) -> list[str]:
    """Return explicit type-qualified candidates for annual/one-shot/special releases."""
    if release_metadata.issue_type != IssueType.ANNUAL:
        if release_metadata.issue_type not in {IssueType.ONE_SHOT, IssueType.SPECIAL}:
            return []
        if not release_metadata.series_name:
            return []
        type_label = release_metadata.issue_type.display_name.strip()
        if not type_label:
            return []
        return [f"{release_metadata.series_name} {type_label}".strip()]

    if not release_metadata.series_name or release_metadata.year is None:
        return []
    normalized_stem = re.sub(r"[_\.]+", " ", Path(file_name).stem)
    normalized_stem = re.sub(r"\s+", " ", normalized_stem).strip()
    if " annual" not in normalized_stem.lower():
        return []

    candidates = [
        f"{release_metadata.series_name} {release_metadata.year} Annual",
        f"{release_metadata.series_name} Annual",
    ]
    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = NameMatcher.normalize(candidate)
        if normalized and normalized not in seen:
            seen.add(normalized)
            deduped.append(candidate)
    return deduped


def _synthetic_one_shot_base_series(
    series_name: str | None,
    release_metadata: SourceMetadata,
) -> str | None:
    """Return the real series title when the import bucket only added ``One Shot``."""
    if release_metadata.issue_type != IssueType.ONE_SHOT:
        return None
    release_series = (release_metadata.series_name or "").strip()
    if not series_name or not release_series:
        return None
    type_label = IssueType.ONE_SHOT.display_name.strip()
    if not type_label:
        return None
    synthetic_label = f"{release_series} {type_label}".strip()
    if NameMatcher.normalize(series_name) != NameMatcher.normalize(synthetic_label):
        return None
    return release_series


def _type_qualified_series_hint(release_metadata: SourceMetadata) -> str | None:
    """Return an explicit ``Series Type`` candidate for collection-family releases."""
    if release_metadata.issue_type not in _TYPE_QUALIFIED_SERIES_HINT_TYPES:
        return None
    if release_metadata.signals.get("issue_type") != MetadataSignal.RELEASE_TITLE:
        return None
    series_name = (release_metadata.series_name or "").strip()
    if not series_name:
        return None
    type_label = release_metadata.issue_type.display_name.strip()
    if not type_label:
        return None
    candidate = f"{series_name} {type_label}".strip()
    if NameMatcher.normalize(candidate) == NameMatcher.normalize(series_name):
        return None
    return candidate


def source_metadata_for_import_file(
    imp_series: ImportedSeries,
    imp_file: ImportedFile,
) -> SourceMetadata:
    """Build shared semantic metadata from persisted import scan fields."""
    diagnostics = dict(imp_file.diagnostics or {})
    parsed_filename = parse_release_title(imp_file.file_name or "")
    raw_issue_type = diagnostics.get("source_issue_type") or (
        dict(imp_series.diagnostics or {}).get("source_issue_type")
    )
    if (
        not raw_issue_type
        and parsed_filename is not None
        and (parsed_filename.issue_number is None or parsed_filename.volume is not None)
    ):
        raw_issue_type = parsed_filename.issue_type.value
    source_diagnostics = diagnostics.get("source_metadata")
    if not isinstance(source_diagnostics, dict):
        source_diagnostics = {}
    source_diagnostics = dict(source_diagnostics)
    volume_hint = _volume_subtitle_hint(imp_file.file_name, diagnostics)
    series_name = imp_file.parsed_series or imp_series.raw_series_name
    issue_number = imp_file.parsed_issue_number
    if issue_number is None:
        issue_number = _filename_parse_issue_number(diagnostics, imp_file.file_name)
    if issue_number is None:
        issue_number = _filename_parse_volume_issue_number(diagnostics, imp_file.file_name)
    if volume_hint is not None:
        source_diagnostics["volume_subtitle_hint"] = volume_hint
        series_name = volume_hint["base_series"]
        issue_number = volume_hint["issue_number"]
    return SourceMetadata(
        original_title=imp_file.file_name,
        source_path=imp_file.file_path,
        series_name=series_name,
        issue_number=issue_number,
        year=imp_file.parsed_year or imp_series.raw_year,
        volume=_filename_parse_volume(diagnostics, imp_file.file_name),
        issue_type=_source_issue_type(raw_issue_type),
        comicvine_issue_id=imp_file.comicvine_issue_id,
        signals=_metadata_signals(diagnostics.get("metadata_signals")),
        diagnostics=source_diagnostics,
    )


async def load_archive_entry_issue_hint_for_import_file(
    imp_file: ImportedFile,
    metadata: SourceMetadata,
) -> SourceMetadata:
    """Load archive page-name issue hints on demand without extracting image payloads."""
    if _archive_entry_issue_hint({"source_metadata": metadata.diagnostics}) is not None:
        return metadata
    if metadata.diagnostics.get("archive_entry_issue_hint_checked") is True:
        return metadata
    if metadata.diagnostics.get("has_comicinfo") is True:
        return metadata
    if metadata.issue_number is None or not metadata.source_path:
        return metadata
    if Path(metadata.source_path).suffix.lower() not in {".cbz", ".cbr", ".cb7"}:
        return metadata

    hint = await asyncio.to_thread(
        SourceMetadataExtractor.archive_entry_issue_hint_from_path,
        metadata.source_path,
        expected_series_name=metadata.series_name,
    )
    source_diagnostics = {**metadata.diagnostics, "archive_entry_issue_hint_checked": True}
    persisted_diagnostics = dict(imp_file.diagnostics or {})
    persisted_source_metadata = persisted_diagnostics.get("source_metadata")
    if not isinstance(persisted_source_metadata, dict):
        persisted_source_metadata = {}
    updated_persisted_source_metadata = {
        **persisted_source_metadata,
        "archive_entry_issue_hint_checked": True,
    }
    if hint is not None:
        source_diagnostics["archive_entry_issue_hint"] = dict(hint)
        updated_persisted_source_metadata["archive_entry_issue_hint"] = dict(hint)
    persisted_diagnostics["source_metadata"] = {
        **updated_persisted_source_metadata,
    }
    imp_file.diagnostics = persisted_diagnostics
    return metadata.model_copy(update={"diagnostics": source_diagnostics})


def import_file_has_deferred_archive_metadata(imp_file: ImportedFile) -> bool:
    """Return True when a file still has deferred archive metadata available."""
    diagnostics = dict(imp_file.diagnostics or {})
    return _has_deferred_archive_metadata(diagnostics.get("source_metadata"))


async def load_deferred_source_metadata_for_import_file(
    imp_series: ImportedSeries,
    imp_file: ImportedFile,
    *,
    include_archive_entry_issue_hint: bool = True,
) -> SourceMetadata:
    """Load archive metadata on demand for a deferred import file."""
    base_metadata = source_metadata_for_import_file(imp_series, imp_file)
    if not import_file_has_deferred_archive_metadata(imp_file):
        return base_metadata

    loaded_metadata = await asyncio.to_thread(
        SourceMetadataExtractor().from_archive_path,
        imp_file.file_path,
        include_archive_entry_issue_hint=include_archive_entry_issue_hint,
    )
    update: dict[str, object] = {}
    if loaded_metadata.series_name is None and base_metadata.series_name is not None:
        update["series_name"] = base_metadata.series_name
    if loaded_metadata.issue_number is None and base_metadata.issue_number is not None:
        update["issue_number"] = base_metadata.issue_number
    if loaded_metadata.year is None and base_metadata.year is not None:
        update["year"] = base_metadata.year
    return loaded_metadata.model_copy(update=update) if update else loaded_metadata


def source_metadata_for_import_series(imp_series: ImportedSeries) -> SourceMetadata:
    """Build shared semantic metadata from persisted import series scan fields."""
    diagnostics = dict(imp_series.diagnostics or {})
    return SourceMetadata(
        original_title=imp_series.raw_series_name,
        source_path=imp_series.source_folder,
        series_name=imp_series.raw_series_name,
        year=imp_series.raw_year,
        issue_type=_source_issue_type(diagnostics.get("source_issue_type")),
        comicvine_series_id=(
            imp_series.cv_id
            if imp_series.cv_match_method in {"mylar3_cv_id", "comicinfo_cv_id"}
            else None
        ),
        series_status=(
            str(diagnostics["series_status"])
            if diagnostics.get("series_status") is not None
            else None
        ),
        issue_count_hint=(
            int(diagnostics["issue_count_hint"])
            if diagnostics.get("issue_count_hint") is not None
            else None
        ),
        diagnostics={
            "file_count": imp_series.file_count,
        },
    )


async def source_metadata_for_matching_series(
    session: AsyncSession,
    imp_series: ImportedSeries,
    *,
    load_deferred_archive_metadata: bool = False,
    trusted_identity_probe_limit: int = 0,
) -> SourceMetadata:
    """Augment persisted series metadata with alternate filename-derived identities."""
    metadata = source_metadata_for_import_series(imp_series)
    result = await session.execute(
        sa_select(ImportedFile)
        .where(ImportedFile.import_series_id == imp_series.id)
        .order_by(ImportedFile.id.asc())
    )
    files = list(result.scalars().all())
    if not files:
        return metadata

    extractor = SourceMetadataExtractor()
    signals = dict(metadata.signals)
    diagnostics = dict(metadata.diagnostics)
    alternate_release_candidates: list[dict[str, object]] = []
    seen_alternates: set[tuple[str, int | None]] = set()
    issue_numbers: list[float] = []
    volume_subtitle_hints: dict[tuple[str, str, float], _VolumeSubtitleHint] = {}
    normalized_series = NameMatcher.normalize(
        metadata.series_name or imp_series.raw_series_name or ""
    )
    clean_one_shot_series_names: list[str] = []
    inferred_issue_types: list[IssueType] = []
    trusted_series_id = metadata.comicvine_series_id
    trusted_series_id_signal = metadata.signals.get("comicvine_series_id")
    trusted_series_id_source = metadata.diagnostics.get("comicvine_series_id_source")
    identity_conflicts: list[dict[str, object]] = []
    probed_archive_count = 0
    for imp_file in files:
        file_diagnostics = dict(imp_file.diagnostics or {})
        file_signals = _metadata_signals(file_diagnostics.get("metadata_signals"))
        persisted_source_metadata = file_diagnostics.get("source_metadata")
        persisted_source_metadata = (
            persisted_source_metadata if isinstance(persisted_source_metadata, dict) else {}
        )
        persisted_conflicts = persisted_source_metadata.get("identity_conflicts")
        if isinstance(persisted_conflicts, list):
            for conflict in persisted_conflicts:
                if isinstance(conflict, dict) and conflict not in identity_conflicts:
                    identity_conflicts.append(dict(conflict))
        persisted_series_id = _optional_int(file_diagnostics.get("comicvine_series_id"))
        persisted_series_signal = file_signals.get("comicvine_series_id")
        if persisted_series_id is not None and persisted_series_signal in {
            MetadataSignal.COMICINFO,
            MetadataSignal.SIDECAR,
        }:
            if trusted_series_id is not None and trusted_series_id != persisted_series_id:
                conflict = {
                    "field": "comicvine_series_id",
                    "first": trusted_series_id,
                    "conflicting": persisted_series_id,
                }
                if conflict not in identity_conflicts:
                    identity_conflicts.append(conflict)
            else:
                trusted_series_id = persisted_series_id
                trusted_series_id_signal = persisted_series_signal
                trusted_series_id_source = (
                    persisted_source_metadata.get("comicvine_series_id_source")
                    or persisted_series_signal.value
                )
        archive_metadata: SourceMetadata | None = None
        has_deferred_archive_metadata = _has_deferred_archive_metadata(
            file_diagnostics.get("source_metadata")
        )
        should_probe_trusted_identity = (
            probed_archive_count < trusted_identity_probe_limit
            and has_deferred_archive_metadata
            and (
                trusted_series_id is None
                or (
                    trusted_series_id_signal == MetadataSignal.SIDECAR and probed_archive_count == 0
                )
            )
        )
        if (
            imp_file.status != ImportedFileStatus.SAFETY_BLOCKED
            and has_deferred_archive_metadata
            and (load_deferred_archive_metadata or should_probe_trusted_identity)
        ):
            archive_metadata = await load_deferred_source_metadata_for_import_file(
                imp_series,
                imp_file,
                include_archive_entry_issue_hint=load_deferred_archive_metadata,
            )
            if should_probe_trusted_identity:
                probed_archive_count += 1

            if archive_metadata.comicvine_series_id is not None:
                refreshed_diagnostics = dict(imp_file.diagnostics or {})
                refreshed_diagnostics.update(
                    sync_import_file_source_metadata(
                        imp_file,
                        Path(imp_file.file_path),
                        archive_metadata,
                    )
                )
                imp_file.diagnostics = refreshed_diagnostics
                file_diagnostics = refreshed_diagnostics
                file_signals = _metadata_signals(file_diagnostics.get("metadata_signals"))

            archive_conflicts = archive_metadata.diagnostics.get("identity_conflicts")
            if isinstance(archive_conflicts, list):
                for conflict in archive_conflicts:
                    if isinstance(conflict, dict) and conflict not in identity_conflicts:
                        identity_conflicts.append(dict(conflict))
            archive_series_id = archive_metadata.comicvine_series_id
            archive_series_signal = archive_metadata.signals.get("comicvine_series_id")
            if archive_series_id is not None and archive_series_signal in {
                MetadataSignal.COMICINFO,
                MetadataSignal.SIDECAR,
            }:
                if trusted_series_id is not None and trusted_series_id != archive_series_id:
                    identity_conflicts.append(
                        {
                            "field": "comicvine_series_id",
                            "first": trusted_series_id,
                            "conflicting": archive_series_id,
                        }
                    )
                else:
                    trusted_series_id = archive_series_id
                    trusted_series_id_signal = archive_series_signal
                    trusted_series_id_source = (
                        archive_metadata.diagnostics.get("comicvine_series_id_source")
                        or archive_series_signal.value
                    )
        if imp_file.parsed_issue_number is not None:
            issue_numbers.append(float(imp_file.parsed_issue_number))
        elif archive_metadata is not None and archive_metadata.issue_number is not None:
            issue_numbers.append(float(archive_metadata.issue_number))
        else:
            filename_issue_number = _filename_parse_issue_number(
                file_diagnostics,
                imp_file.file_name,
            )
            volume_issue_number = _filename_parse_volume_issue_number(file_diagnostics)
            if filename_issue_number is not None:
                issue_numbers.append(filename_issue_number)
            elif volume_issue_number is not None:
                issue_numbers.append(volume_issue_number)

        if "series_name" not in signals:
            parsed_series = (imp_file.parsed_series or "").strip()
            series_signal = file_signals.get("series_name")
            if archive_metadata is not None and archive_metadata.series_name:
                parsed_series = archive_metadata.series_name.strip()
                series_signal = archive_metadata.signals.get("series_name", series_signal)
            if (
                parsed_series
                and series_signal in {MetadataSignal.COMICINFO, MetadataSignal.SIDECAR}
                and NameMatcher.normalize(parsed_series) == normalized_series
            ):
                signals["series_name"] = series_signal
                file_year = imp_file.parsed_year
                if (
                    "year" not in signals
                    and (
                        archive_metadata.signals.get("year")
                        if archive_metadata is not None
                        else file_signals.get("year")
                    )
                    in {
                        MetadataSignal.COMICINFO,
                        MetadataSignal.SIDECAR,
                    }
                    and file_year is not None
                    and metadata.year == file_year
                ):
                    year_signal = (
                        archive_metadata.signals.get("year")
                        if archive_metadata is not None
                        else file_signals["year"]
                    )
                    if year_signal is not None:
                        signals["year"] = year_signal
                source_metadata = file_diagnostics.get("source_metadata")
                merged_source_metadata: dict[str, object] = {}
                if isinstance(source_metadata, dict):
                    merged_source_metadata.update(source_metadata)
                if archive_metadata is not None:
                    merged_source_metadata.update(archive_metadata.diagnostics)
                if merged_source_metadata:
                    comicinfo = merged_source_metadata.get("comicinfo")
                    if isinstance(comicinfo, dict) and "comicinfo" not in diagnostics:
                        diagnostics["comicinfo"] = dict(comicinfo)

        release_metadata = extractor.from_release_title(
            imp_file.file_name,
            source_path=imp_file.file_path,
            folder_name=Path(imp_file.file_path).parent.name,
        )
        file_issue_type = _source_issue_type(file_diagnostics.get("source_issue_type"))
        if file_issue_type == IssueType.ISSUE and release_metadata.issue_type != IssueType.ISSUE:
            file_issue_type = release_metadata.issue_type
        if file_issue_type != IssueType.ISSUE and file_issue_type not in inferred_issue_types:
            inferred_issue_types.append(file_issue_type)
        clean_one_shot_series = _synthetic_one_shot_base_series(
            metadata.series_name,
            release_metadata,
        )
        if clean_one_shot_series and clean_one_shot_series not in clean_one_shot_series_names:
            clean_one_shot_series_names.append(clean_one_shot_series)
        for annual_series in _issue_like_series_hints(imp_file.file_name, release_metadata):
            normalized_annual = NameMatcher.normalize(annual_series)
            annual_key = (normalized_annual, release_metadata.year)
            if (
                normalized_annual
                and normalized_annual != normalized_series
                and annual_key not in seen_alternates
            ):
                seen_alternates.add(annual_key)
                alternate_release_candidates.append(
                    {
                        "series_name": annual_series,
                        "year": release_metadata.year,
                        "file_name": imp_file.file_name,
                        "signal": MetadataSignal.RELEASE_TITLE.value,
                        "issue_type": release_metadata.issue_type.value,
                        "issue_type_qualified": True,
                    }
                )
        type_qualified_series = _type_qualified_series_hint(release_metadata)
        if type_qualified_series is not None:
            normalized_type_qualified = NameMatcher.normalize(type_qualified_series)
            type_qualified_key = (normalized_type_qualified, release_metadata.year)
            if (
                normalized_type_qualified
                and normalized_type_qualified != normalized_series
                and type_qualified_key not in seen_alternates
            ):
                seen_alternates.add(type_qualified_key)
                alternate_release_candidates.append(
                    {
                        "series_name": type_qualified_series,
                        "year": release_metadata.year,
                        "file_name": imp_file.file_name,
                        "signal": MetadataSignal.RELEASE_TITLE.value,
                        "issue_type": release_metadata.issue_type.value,
                        "issue_type_qualified": True,
                    }
                )
        volume_hint = _volume_subtitle_hint(imp_file.file_name, file_diagnostics)
        if volume_hint is not None:
            volume_subtitle_hints.setdefault(
                (
                    volume_hint["base_series"],
                    volume_hint["subtitle"],
                    volume_hint["issue_number"],
                ),
                volume_hint,
            )
            alternate_series = str(volume_hint["base_series"])
            normalized_alternate = NameMatcher.normalize(alternate_series)
            alt_key = (normalized_alternate, release_metadata.year)
            if (
                normalized_alternate
                and normalized_alternate != normalized_series
                and alt_key not in seen_alternates
            ):
                seen_alternates.add(alt_key)
                alternate_release_candidates.append(
                    {
                        "series_name": alternate_series,
                        "year": release_metadata.year,
                        "file_name": imp_file.file_name,
                        "signal": MetadataSignal.RELEASE_TITLE.value,
                        "issue_title_hint": volume_hint["subtitle"],
                        "volume_issue_number": volume_hint["issue_number"],
                    }
                )
        alternate_series = (release_metadata.series_name or "").strip()
        if not alternate_series:
            continue
        normalized_alternate = NameMatcher.normalize(alternate_series)
        if not normalized_alternate or normalized_alternate == normalized_series:
            continue
        alt_key = (normalized_alternate, release_metadata.year)
        if alt_key in seen_alternates:
            continue
        seen_alternates.add(alt_key)
        alternate_release_candidates.append(
            {
                "series_name": alternate_series,
                "year": release_metadata.year,
                "file_name": imp_file.file_name,
                "signal": MetadataSignal.RELEASE_TITLE.value,
            }
        )

    if alternate_release_candidates:
        diagnostics["alternate_release_candidates"] = alternate_release_candidates
    if len(volume_subtitle_hints) == 1:
        diagnostics["volume_subtitle_hint"] = next(iter(volume_subtitle_hints.values()))
    if issue_numbers:
        diagnostics["issue_numbers"] = sorted(set(issue_numbers))
    if trusted_series_id_source is not None:
        diagnostics["comicvine_series_id_source"] = trusted_series_id_source
    if identity_conflicts:
        diagnostics["identity_conflicts"] = identity_conflicts

    metadata_update: dict[str, object] = {}
    if signals != metadata.signals:
        metadata_update["signals"] = signals
    if diagnostics != metadata.diagnostics:
        metadata_update["diagnostics"] = diagnostics
    if metadata.issue_number is None and issue_numbers:
        metadata_update["issue_number"] = sorted(issue_numbers)[0]
    if trusted_series_id is not None and not identity_conflicts:
        metadata_update["comicvine_series_id"] = trusted_series_id
        if trusted_series_id_signal is not None:
            signals["comicvine_series_id"] = trusted_series_id_signal
            metadata_update["signals"] = signals
    if len(clean_one_shot_series_names) == 1:
        metadata_update["series_name"] = clean_one_shot_series_names[0]
    if metadata.issue_type == IssueType.ISSUE and len(inferred_issue_types) == 1:
        metadata_update["issue_type"] = inferred_issue_types[0]
    if metadata_update:
        return metadata.model_copy(update=metadata_update)
    return metadata


def build_import_metadata_conflict(
    *,
    metadata: SourceMetadata,
    target_series_title: str,
    target_series_year: int | None,
    target_issue_number: float | None,
    target_issue_cv_id: int | None,
    target_issue_title: str | None,
) -> dict[str, Any] | None:
    """Explain why import refused an auto-match due to conflicting strong signals."""
    archive_hint = _archive_entry_issue_hint({"source_metadata": metadata.diagnostics})
    archive_issue_number = (
        normalize_issue_number(archive_hint.get("issue_number")) if archive_hint else None
    )
    if (
        archive_hint is not None
        and archive_issue_number is not None
        and target_issue_number is not None
        and not _issue_numbers_equal(archive_issue_number, target_issue_number)
    ):
        source_issue_number = metadata.issue_number
        return {
            "kind": "metadata_conflict",
            "conflict_type": "archive_entry_issue_number_mismatch",
            "preserve_series_match": True,
            "rejection_reason": (
                f"Filename parsed as issue #{_format_issue_number(source_issue_number)}, "
                "but archive page names strongly suggest issue "
                f"#{_format_issue_number(archive_issue_number)}."
            ),
            "source_series": metadata.series_name,
            "source_year": metadata.year,
            "source_issue_number": source_issue_number,
            "target_series": target_series_title,
            "target_series_year": target_series_year,
            "target_issue_number": target_issue_number,
            "target_issue_title": target_issue_title,
            "target_issue_cv_id": target_issue_cv_id,
            "suggested_issue_number": archive_issue_number,
            "archive_entry_issue_hint": archive_hint,
        }

    if (
        metadata.comicvine_issue_id is None
        or target_issue_cv_id is None
        or metadata.comicvine_issue_id != target_issue_cv_id
    ):
        return None

    source_series = (metadata.series_name or "").strip()
    if not source_series:
        return None

    series_signal = metadata.signals.get("series_name")
    if series_signal not in {MetadataSignal.COMICINFO, MetadataSignal.RELEASE_TITLE}:
        return None

    match = _matcher.match(source_series, target_series_title)
    if match.match_type in {"exact", "alternate", "token_set", "starts_with", "token_subset"}:
        return None

    source_comicinfo = metadata.diagnostics.get("comicinfo")
    if not isinstance(source_comicinfo, dict):
        source_comicinfo = {}

    return {
        "kind": "metadata_conflict",
        "conflict_type": "comicinfo_issue_id_series_mismatch",
        "rejection_reason": (
            f"ComicInfo issue ID points to '{target_series_title}', "
            f"but the source series title is '{source_series}'."
        ),
        "source_series": source_series,
        "source_year": metadata.year,
        "source_series_signal": series_signal.value,
        "target_series": target_series_title,
        "target_series_year": target_series_year,
        "target_issue_number": target_issue_number,
        "target_issue_title": target_issue_title,
        "target_issue_cv_id": target_issue_cv_id,
        "source_issue_cv_id": metadata.comicvine_issue_id,
        "series_similarity": round(match.similarity, 4),
        "series_match_type": match.match_type,
        "comicinfo": source_comicinfo,
    }


def sync_import_file_source_metadata(
    imp_file: ImportedFile,
    source_path: Path,
    metadata: SourceMetadata,
) -> dict[str, object]:
    """Refresh persisted import-file metadata from a repaired archive."""
    existing_diagnostics = dict(imp_file.diagnostics or {})
    existing_source_metadata = existing_diagnostics.get("source_metadata")
    existing_source_metadata = (
        existing_source_metadata if isinstance(existing_source_metadata, dict) else {}
    )
    source_metadata = dict(metadata.diagnostics)
    filename_parse = existing_source_metadata.get("filename_parse")
    if not isinstance(filename_parse, dict):
        original_filename_parse = parse_release_title(imp_file.file_name)
        filename_parse = {
            "series_name": (
                original_filename_parse.series_name if original_filename_parse else None
            ),
            "issue_number": (
                original_filename_parse.issue_number if original_filename_parse else None
            ),
            "year": original_filename_parse.year if original_filename_parse else None,
            "volume": original_filename_parse.volume if original_filename_parse else None,
            "issue_type": (
                original_filename_parse.issue_type.value
                if original_filename_parse is not None
                else None
            ),
        }
    source_metadata["filename_parse"] = filename_parse
    metadata_signals = {key: value.value for key, value in metadata.signals.items()}

    imp_file.file_path = str(source_path)
    imp_file.file_name = source_path.name
    imp_file.file_size = source_path.stat().st_size
    imp_file.file_format = source_path.suffix.lower().lstrip(".")
    imp_file.parsed_series = metadata.series_name
    imp_file.parsed_issue_number = metadata.issue_number
    imp_file.parsed_year = metadata.year
    imp_file.has_comicinfo = bool(source_metadata.get("has_comicinfo"))
    imp_file.comicvine_issue_id = metadata.comicvine_issue_id

    comicinfo = source_metadata.get("comicinfo")
    if imp_file.has_comicinfo and isinstance(comicinfo, dict):
        imp_file.issue_number_raw = comicinfo.get("number")

    return {
        "source_issue_type": metadata.issue_type.value,
        "comicvine_series_id": metadata.comicvine_series_id,
        "metadata_signals": metadata_signals,
        "source_metadata": source_metadata,
    }
