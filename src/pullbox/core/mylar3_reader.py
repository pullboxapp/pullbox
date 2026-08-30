"""Mylar3 database reader for collection import.

Reads series data from a Mylar3 SQLite database (mylar.db) and converts
records to DiscoveredSeries instances, preserving ComicVine IDs for
high-confidence matching during the import identification phase.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import structlog

from pullbox.core.collection_scanner import COMIC_EXTENSIONS, DiscoveredFile, DiscoveredSeries
from pullbox.core.exceptions import MylarReadError
from pullbox.core.issue_numbers import format_issue_number
from pullbox.core.library_layout import (
    ImportLayoutMode,
    SourceLayoutMatch,
    SourceLayoutSpec,
    compile_source_layout,
    resolve_source_layout_spec,
)
from pullbox.core.naming import parse_filename
from pullbox.core.naming_type_detection import detect_issue_type
from pullbox.core.release_parser import normalize_issue_number
from pullbox.core.source_metadata import MetadataSignal
from pullbox.models.issue import IssueType

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _MylarIssueRecord:
    """Trusted issue identity read from Mylar's issue tables."""

    issue_id: int
    series_cv_id: int
    issue_number: str | None
    title: str | None
    release_date: str | None
    location: str | None
    issue_type: IssueType = IssueType.ISSUE
    series_name: str | None = None


@dataclass(slots=True)
class _MylarReleaseSeries:
    """Files attached to a separate ComicVine release series in Mylar."""

    cv_id: int
    name: str
    year: int | None
    publisher: str | None
    source_folder: str
    source_folder_relative: str
    series_status: str | None
    files: list[DiscoveredFile]


@dataclass(frozen=True, slots=True)
class _ResolvedMylarPath:
    """Safe resolution state for one Mylar ComicLocation."""

    path: Path | None
    status: str
    mapping_applied: bool
    reason: str | None = None
    rejection_reason: str | None = None


class Mylar3Reader:
    """Reads series data from a Mylar3 SQLite database.

    Extracts series records and converts them to DiscoveredSeries instances,
    preserving ComicVine IDs for high-confidence matching in ImportService.

    Args:
        db_path: Path to mylar.db file.
        path_map: Optional dict of {container_prefix: host_prefix}.
                  Applied to ComicLocation before checking disk.
    """

    MYLAR3_CV_PREFIX = "CV-"

    def __init__(
        self,
        db_path: str | Path,
        path_map: dict[str, str] | None = None,
        source_layout: SourceLayoutSpec | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._path_map = path_map or {}
        self._source_layout = resolve_source_layout_spec(source_layout or SourceLayoutSpec())
        self._compiled_source_layout = (
            None
            if self._source_layout.mode == ImportLayoutMode.AUTO
            else compile_source_layout(self._source_layout)
        )

    async def read_series(self) -> list[DiscoveredSeries]:
        """Read all series from the Mylar3 database.

        Returns a list of DiscoveredSeries with mylar3_cv_id populated
        wherever a ComicVine ID exists in Mylar3.

        Raises:
            FileNotFoundError: If db_path does not exist.
            MylarReadError: If the file is not a valid Mylar3 database.
        """
        if not self._db_path.exists():
            msg = f"Mylar3 database not found: {self._db_path}"
            raise FileNotFoundError(msg)

        return await asyncio.to_thread(self._read_sync)

    def _read_sync(self) -> list[DiscoveredSeries]:
        """Synchronous database read — runs in a thread."""
        try:
            conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.DatabaseError as exc:
            msg = f"Could not read Mylar3 database: {exc}"
            raise MylarReadError(msg) from exc

        try:
            # Verify the comics table exists
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='comics'"
            )
            if not cursor.fetchone():
                msg = "Not a Mylar3 database: 'comics' table not found"
                raise MylarReadError(msg)

            rows = conn.execute(
                "SELECT ComicID, ComicName, ComicYear, ComicPublisher, "
                "ComicLocation, Status, Total FROM comics"
            ).fetchall()
            issue_records = self._read_issue_records(conn)
        except sqlite3.DatabaseError as exc:
            msg = f"Could not read Mylar3 database: {exc}"
            raise MylarReadError(msg) from exc
        finally:
            conn.close()

        return self._convert_rows(rows, issue_records)

    def _convert_rows(
        self,
        rows: list[sqlite3.Row],
        issue_records: dict[int, list[_MylarIssueRecord]],
    ) -> list[DiscoveredSeries]:
        """Convert Mylar3 database rows to DiscoveredSeries instances."""
        results: list[DiscoveredSeries] = []
        seen_cv_ids: set[int] = set()
        release_series: dict[int, _MylarReleaseSeries] = {}

        for row in rows:
            cv_id = self._parse_cv_id(row["ComicID"])

            # Deduplicate by ComicVine ID
            if cv_id is not None and cv_id in seen_cv_ids:
                logger.warning(
                    "mylar3_duplicate_cv_id",
                    cv_id=cv_id,
                    series_name=row["ComicName"],
                )
                continue
            if cv_id is not None:
                seen_cv_ids.add(cv_id)

            year = self._parse_year(row["ComicYear"])
            location = row["ComicLocation"]
            issue_count_hint = self._parse_positive_int(row["Total"])
            series_status = row["Status"]
            path_resolution = self._resolve_location_details(location)
            resolved_location = (
                str(path_resolution.path) if path_resolution.path is not None else None
            )
            file_count = self._get_resolved_file_count(path_resolution.path)
            has_files = file_count > 0
            series_issue_records = issue_records.get(cv_id, []) if cv_id is not None else []

            # Build sample paths and DiscoveredFile objects from the location directory
            sample_paths: list[str] = []
            discovered_files: list[DiscoveredFile] = []
            if resolved_location:
                p = Path(resolved_location)
                if p.is_dir():
                    comic_paths = sorted(
                        f for f in p.iterdir() if f.suffix.lower() in COMIC_EXTENSIONS
                    )
                    sample_paths = [str(f) for f in comic_paths[:5]]
                    discovered_files = self._build_files(
                        comic_paths,
                        source_location=location,
                        series_name=row["ComicName"] or "Unknown",
                        series_year=year,
                        series_publisher=row["ComicPublisher"],
                        issue_records=series_issue_records,
                        issue_count_hint=issue_count_hint,
                        series_status=series_status,
                    )

            if cv_id is not None and discovered_files:
                parent_files: list[DiscoveredFile] = []
                records_by_issue_id = {record.issue_id: record for record in series_issue_records}
                for discovered_file in discovered_files:
                    release_cv_id = discovered_file.comicvine_series_id
                    if release_cv_id is None or release_cv_id == cv_id:
                        parent_files.append(discovered_file)
                        continue
                    issue_record = (
                        records_by_issue_id.get(discovered_file.comicvine_issue_id)
                        if discovered_file.comicvine_issue_id is not None
                        else None
                    )
                    if issue_record is None or issue_record.issue_type != IssueType.ANNUAL:
                        parent_files.append(discovered_file)
                        continue
                    release = release_series.get(release_cv_id)
                    if release is None:
                        release = _MylarReleaseSeries(
                            cv_id=release_cv_id,
                            name=(
                                issue_record.series_name
                                or discovered_file.parsed_series
                                or f"{row['ComicName'] or 'Unknown'} Annual"
                            ),
                            year=self._release_year(
                                issue_record.release_date,
                                discovered_file.parsed_year,
                            ),
                            publisher=row["ComicPublisher"],
                            source_folder=resolved_location or "",
                            source_folder_relative=location or "",
                            series_status=series_status,
                            files=[],
                        )
                        release_series[release_cv_id] = release
                    release.files.append(discovered_file)
                discovered_files = parent_files
                sample_paths = [item.file_path for item in discovered_files[:5]]
                file_count = len(discovered_files)
                has_files = bool(discovered_files)

            diagnostics: dict[str, object] = {}
            diagnostics["mylar3_path"] = {
                "status": path_resolution.status,
                "mapping_applied": path_resolution.mapping_applied,
            }
            if path_resolution.reason is not None:
                diagnostics.update(
                    {
                        "kind": "mylar3_path_incompatible",
                        "reason": path_resolution.reason,
                        "rejection_reason": path_resolution.rejection_reason,
                    }
                )
            if series_status:
                diagnostics["series_status"] = series_status
            if issue_count_hint is not None:
                diagnostics["issue_count_hint"] = issue_count_hint

            results.append(
                DiscoveredSeries(
                    raw_series_name=row["ComicName"] or "Unknown",
                    raw_year=year,
                    raw_publisher=row["ComicPublisher"],
                    file_count=len(discovered_files) if discovered_files else file_count,
                    sample_paths=sample_paths,
                    source_folder=resolved_location or "",
                    source_folder_relative=location or "",
                    has_files=has_files,
                    files=discovered_files,
                    mylar3_cv_id=cv_id,
                    diagnostics=diagnostics,
                )
            )

        results_by_cv_id = {
            result.mylar3_cv_id: result for result in results if result.mylar3_cv_id is not None
        }
        for release_cv_id, release in release_series.items():
            existing = results_by_cv_id.get(release_cv_id)
            if existing is not None:
                known_paths = {item.file_path for item in existing.files}
                existing.files.extend(
                    item for item in release.files if item.file_path not in known_paths
                )
                existing.file_count = len(existing.files)
                existing.sample_paths = [item.file_path for item in existing.files[:5]]
                existing.has_files = bool(existing.files)
                existing.diagnostics["source_issue_type"] = IssueType.ANNUAL.value
                continue

            release_diagnostics: dict[str, object] = {
                "issue_count_hint": len(release.files),
                "source_issue_type": IssueType.ANNUAL.value,
            }
            if release.series_status:
                release_diagnostics["series_status"] = release.series_status
            discovered = DiscoveredSeries(
                raw_series_name=release.name,
                raw_year=release.year,
                raw_publisher=release.publisher,
                file_count=len(release.files),
                sample_paths=[item.file_path for item in release.files[:5]],
                source_folder=release.source_folder,
                source_folder_relative=release.source_folder_relative,
                has_files=bool(release.files),
                files=release.files,
                mylar3_cv_id=release.cv_id,
                diagnostics=release_diagnostics,
            )
            results.append(discovered)
            results_by_cv_id[release_cv_id] = discovered

        logger.info(
            "mylar3_read_completed",
            total_series=len(results),
            with_cv_id=sum(1 for r in results if r.mylar3_cv_id is not None),
        )

        return results

    def _release_year(self, release_date: str | None, parsed_year: int | None) -> int | None:
        if release_date and len(release_date) >= 4:
            parsed = self._parse_year(release_date[:4])
            if parsed is not None:
                return parsed
        return parsed_year

    def _parse_cv_id(self, comic_id: str | None) -> int | None:
        """Parse 'CV-47050' to 47050. Returns None if malformed."""
        if not comic_id:
            return None
        if comic_id.startswith(self.MYLAR3_CV_PREFIX):
            try:
                return int(comic_id[len(self.MYLAR3_CV_PREFIX) :])
            except ValueError:
                return None
        # Try parsing as plain integer
        try:
            return int(comic_id)
        except ValueError:
            return None

    def _parse_year(self, year_str: str | None) -> int | None:
        """Parse year string, handling NULL, '0000', and 4-digit strings."""
        if not year_str:
            return None
        try:
            year = int(year_str)
            if year == 0:
                return None
            return year
        except ValueError:
            return None

    def _parse_positive_int(self, raw_value: object) -> int | None:
        if raw_value is None:
            return None
        try:
            value = int(str(raw_value).strip())
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def _resolve_location(self, location: str | None) -> str | None:
        """Apply path map translation to a ComicLocation string."""
        resolved = self._resolve_location_details(location)
        return str(resolved.path) if resolved.path is not None else None

    def _resolve_location_details(self, location: str | None) -> _ResolvedMylarPath:
        """Resolve one location without allowing path-map escape or silent misses."""
        if not location:
            return _ResolvedMylarPath(
                path=None,
                status="missing",
                mapping_applied=False,
                reason="missing_location",
                rejection_reason="Mylar does not provide a comic folder for this series.",
            )
        location_path = Path(location)
        if not location_path.is_absolute():
            return _ResolvedMylarPath(
                path=None,
                status="invalid",
                mapping_applied=False,
                reason="invalid_location",
                rejection_reason="The Mylar comic folder must be an absolute path.",
            )
        for container_prefix, host_prefix in self._ordered_path_map_items():
            container_root = Path(container_prefix)
            try:
                relative = location_path.relative_to(container_root)
            except ValueError:
                continue
            if ".." in relative.parts:
                return _ResolvedMylarPath(
                    path=None,
                    status="invalid",
                    mapping_applied=True,
                    reason="unsafe_path_mapping",
                    rejection_reason=(
                        "The Mylar comic folder resolves outside the configured mapped root."
                    ),
                )
            host_root = Path(host_prefix)
            if not container_root.is_absolute() or not host_root.is_absolute():
                return _ResolvedMylarPath(
                    path=None,
                    status="invalid",
                    mapping_applied=True,
                    reason="invalid_path_mapping",
                    rejection_reason="Mylar path mappings must use absolute paths.",
                )
            resolved_root = host_root.resolve(strict=False)
            resolved_path = (host_root / relative).resolve(strict=False)
            if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
                return _ResolvedMylarPath(
                    path=None,
                    status="invalid",
                    mapping_applied=True,
                    reason="unsafe_path_mapping",
                    rejection_reason=(
                        "The Mylar comic folder resolves outside the configured mapped root."
                    ),
                )
            if not resolved_path.is_dir():
                return _ResolvedMylarPath(
                    path=None,
                    status="missing",
                    mapping_applied=True,
                    reason="mapped_path_missing",
                    rejection_reason=("The mapped Mylar comic folder is not available to Pullbox."),
                )
            return _ResolvedMylarPath(
                path=resolved_path,
                status="mapped",
                mapping_applied=True,
            )

        resolved_local_path = location_path.resolve(strict=False)
        if resolved_local_path.is_dir():
            return _ResolvedMylarPath(
                path=resolved_local_path,
                status="local",
                mapping_applied=False,
            )
        if self._path_map:
            return _ResolvedMylarPath(
                path=None,
                status="unmapped",
                mapping_applied=False,
                reason="unmapped_path",
                rejection_reason=(
                    "The Mylar comic folder is not available through the configured path mappings."
                ),
            )
        return _ResolvedMylarPath(
            path=None,
            status="missing",
            mapping_applied=False,
            reason="path_missing",
            rejection_reason="The Mylar comic folder is not available to Pullbox.",
        )

    def _ordered_path_map_items(self) -> list[tuple[str, str]]:
        """Return path mappings with the most-specific container prefix first."""
        return sorted(
            self._path_map.items(),
            key=lambda item: len(Path(item[0]).parts),
            reverse=True,
        )

    def _build_files(
        self,
        comic_paths: list[Path],
        *,
        source_location: str | None,
        series_name: str,
        series_year: int | None,
        series_publisher: str | None,
        issue_records: list[_MylarIssueRecord],
        issue_count_hint: int | None,
        series_status: str | None,
    ) -> list[DiscoveredFile]:
        """Build DiscoveredFile objects from a list of comic file paths."""
        results: list[DiscoveredFile] = []
        issue_by_file_name = self._issue_records_by_file_name(issue_records)
        for fpath in comic_paths:
            file_name = fpath.name
            file_format = fpath.suffix.lstrip(".").lower()
            issue_record = issue_by_file_name.get(file_name.casefold())
            try:
                file_size = fpath.stat().st_size
            except OSError:
                file_size = 0

            parsed = parse_filename(file_name)
            parsed_series: str | None = series_name
            parsed_issue_number: float | None = None
            parsed_year: int | None = series_year
            parsed_publisher: str | None = series_publisher
            issue_type = IssueType.ISSUE
            issue_number_raw: str | None = None
            metadata_signals: dict[str, str] = {
                "series_name": MetadataSignal.MYLAR3.value,
            }
            if series_year is not None:
                metadata_signals["year"] = MetadataSignal.MYLAR3.value
            if series_publisher is not None:
                metadata_signals["publisher"] = MetadataSignal.MYLAR3.value
            if parsed:
                parsed_issue_number = parsed.issue_number
                try:
                    issue_type = IssueType(parsed.issue_type)
                except ValueError:
                    issue_type = IssueType.ISSUE
                issue_number_raw = format_issue_number(parsed.issue_number)
                if parsed_issue_number is not None:
                    metadata_signals["issue_number"] = MetadataSignal.RELEASE_TITLE.value
                if parsed.issue_type != IssueType.ISSUE.value:
                    metadata_signals["issue_type"] = MetadataSignal.RELEASE_TITLE.value

            if issue_record is not None and issue_record.issue_number:
                issue_number_raw = issue_record.issue_number
                normalized_issue_number = normalize_issue_number(issue_record.issue_number)
                if normalized_issue_number is not None:
                    parsed_issue_number = normalized_issue_number
                    metadata_signals["issue_number"] = MetadataSignal.MYLAR3.value

            metadata_diagnostics: dict[str, object] = {}
            comicvine_issue_id: int | None = None
            comicvine_series_id: int | None = None
            if issue_record is not None:
                if issue_record.issue_type != IssueType.ISSUE:
                    issue_type = issue_record.issue_type
                    metadata_signals["issue_type"] = MetadataSignal.MYLAR3.value
                if issue_record.series_name:
                    parsed_series = issue_record.series_name
                if issue_record.issue_type == IssueType.ANNUAL:
                    parsed_year = self._release_year(issue_record.release_date, parsed_year)
                comicvine_issue_id = issue_record.issue_id
                comicvine_series_id = issue_record.series_cv_id
                metadata_signals["comicvine_issue_id"] = "mylar3"
                metadata_signals["comicvine_series_id"] = "mylar3"
                metadata_diagnostics["mylar3_issue"] = {
                    "issue_id": issue_record.issue_id,
                    "issue_number": issue_record.issue_number,
                    "title": issue_record.title,
                    "release_date": issue_record.release_date,
                }

            layout_match, relative_path = self._match_selected_layout(
                fpath,
                source_location=source_location,
            )
            if self._compiled_source_layout is not None and relative_path is not None:
                layout_diagnostics: dict[str, object] = {
                    "fit": layout_match is not None,
                    "fallback_used": (
                        layout_match is None and self._source_layout.fallback_to_auto
                    ),
                    "relative_path": relative_path,
                }
                if layout_match is None and not self._source_layout.fallback_to_auto:
                    layout_diagnostics.update(
                        {
                            "review_required": True,
                            "review_reason": "selected_layout_no_match",
                        }
                    )
                if layout_match is not None and layout_match.issue_title is not None:
                    layout_diagnostics["issue_title"] = layout_match.issue_title
                metadata_diagnostics["source_layout"] = layout_diagnostics

            if layout_match is not None:
                (
                    parsed_series,
                    parsed_year,
                    parsed_publisher,
                    parsed_issue_number,
                    issue_number_raw,
                    issue_type,
                ) = self._apply_layout_match(
                    layout_match,
                    parsed_series=parsed_series,
                    parsed_year=parsed_year,
                    parsed_publisher=parsed_publisher,
                    parsed_issue_number=parsed_issue_number,
                    issue_number_raw=issue_number_raw,
                    issue_type=issue_type,
                    metadata_signals=metadata_signals,
                    metadata_diagnostics=metadata_diagnostics,
                )

            results.append(
                DiscoveredFile(
                    file_path=str(fpath),
                    file_name=file_name,
                    file_size=file_size,
                    file_format=file_format,
                    parsed_series=parsed_series,
                    parsed_issue_number=parsed_issue_number,
                    parsed_year=parsed_year,
                    parsed_publisher=parsed_publisher,
                    has_comicinfo=False,
                    comicvine_issue_id=comicvine_issue_id,
                    issue_number_raw=issue_number_raw,
                    issue_type=issue_type,
                    comicvine_series_id=comicvine_series_id,
                    series_status=series_status,
                    issue_count_hint=issue_count_hint,
                    metadata_signals=metadata_signals,
                    metadata_diagnostics=metadata_diagnostics,
                )
            )
        return results

    def _match_selected_layout(
        self,
        path: Path,
        *,
        source_location: str | None,
    ) -> tuple[SourceLayoutMatch | None, str | None]:
        """Match one resolved Mylar file against the frozen source layout."""
        compiled = self._compiled_source_layout
        if compiled is None:
            return None, None
        root = self._mapped_source_root(source_location)
        if root is None:
            resolved_location = self._resolve_location(source_location)
            if resolved_location is None:
                return None, None
            root = Path(resolved_location)
            for _segment in compiled.path_segments:
                root = root.parent
        try:
            relative_path = path.relative_to(root).as_posix()
        except ValueError:
            return None, None
        return compiled.match(relative_path), relative_path

    def _mapped_source_root(self, source_location: str | None) -> Path | None:
        """Return the mapped host root that contains one Mylar series path."""
        if not source_location:
            return None
        location_path = Path(source_location)
        for container_prefix, host_prefix in self._ordered_path_map_items():
            try:
                location_path.relative_to(Path(container_prefix))
            except ValueError:
                continue
            return Path(host_prefix)
        return None

    def _apply_layout_match(
        self,
        layout_match: SourceLayoutMatch,
        *,
        parsed_series: str | None,
        parsed_year: int | None,
        parsed_publisher: str | None,
        parsed_issue_number: float | None,
        issue_number_raw: str | None,
        issue_type: IssueType,
        metadata_signals: dict[str, str],
        metadata_diagnostics: dict[str, object],
    ) -> tuple[str | None, int | None, str | None, float | None, str | None, IssueType]:
        """Apply lower-precedence layout evidence without replacing Mylar identity."""
        conflicts: dict[str, dict[str, object]] = {}

        def selected_value(field_name: str, current: object, selected: object | None) -> object:
            if selected is None:
                return current
            current_signal = metadata_signals.get(field_name)
            if current is not None and current_signal == MetadataSignal.MYLAR3.value:
                if str(current).casefold() != str(selected).casefold():
                    conflicts[field_name] = {
                        "selected": selected,
                        "preserved_signal": MetadataSignal.MYLAR3.value,
                    }
                return current
            metadata_signals[field_name] = MetadataSignal.SOURCE_LAYOUT.value
            return selected

        parsed_series = cast(
            "str | None",
            selected_value("series_name", parsed_series, layout_match.series),
        )
        parsed_year = cast(
            "int | None",
            selected_value("year", parsed_year, layout_match.year),
        )
        parsed_publisher = cast(
            "str | None",
            selected_value(
                "publisher",
                parsed_publisher,
                layout_match.publisher,
            ),
        )
        if layout_match.issue_number is not None:
            normalized = normalize_issue_number(layout_match.issue_number)
            parsed_issue_number = cast(
                "float | None",
                selected_value(
                    "issue_number",
                    parsed_issue_number,
                    normalized,
                ),
            )
            if metadata_signals.get("issue_number") == MetadataSignal.SOURCE_LAYOUT.value:
                issue_number_raw = layout_match.issue_number
        if layout_match.issue_type is not None:
            selected_issue_type = IssueType(detect_issue_type(layout_match.issue_type))
            issue_type = cast(
                "IssueType",
                selected_value("issue_type", issue_type, selected_issue_type),
            )
        if conflicts:
            metadata_diagnostics["source_layout_conflicts"] = conflicts
        return (
            parsed_series,
            parsed_year,
            parsed_publisher,
            parsed_issue_number,
            issue_number_raw,
            issue_type,
        )

    def _issue_records_by_file_name(
        self,
        issue_records: list[_MylarIssueRecord],
    ) -> dict[str, _MylarIssueRecord]:
        records: dict[str, _MylarIssueRecord] = {}
        for issue_record in issue_records:
            if not issue_record.location:
                continue
            records[Path(issue_record.location).name.casefold()] = issue_record
        return records

    def _read_issue_records(
        self,
        conn: sqlite3.Connection,
    ) -> dict[int, list[_MylarIssueRecord]]:
        """Read trusted Mylar issue identities keyed by owning ComicID."""
        records: dict[int, list[_MylarIssueRecord]] = {}
        self._append_issue_table_records(conn, records)
        self._append_annual_table_records(conn, records)
        return records

    def _append_issue_table_records(
        self,
        conn: sqlite3.Connection,
        records: dict[int, list[_MylarIssueRecord]],
    ) -> None:
        if not self._table_has_columns(
            conn,
            "issues",
            {"IssueID", "ComicID", "Issue_Number", "IssueName", "IssueDate", "Location"},
        ):
            return
        for row in conn.execute(
            "SELECT IssueID, ComicID, Issue_Number, IssueName, IssueDate, Location FROM issues"
        ).fetchall():
            comic_id = self._parse_cv_id(row["ComicID"])
            issue_id = self._parse_positive_int(row["IssueID"])
            if comic_id is None or issue_id is None:
                continue
            records.setdefault(comic_id, []).append(
                _MylarIssueRecord(
                    issue_id=issue_id,
                    series_cv_id=comic_id,
                    issue_number=row["Issue_Number"],
                    title=row["IssueName"],
                    release_date=row["IssueDate"],
                    location=row["Location"],
                )
            )

    def _append_annual_table_records(
        self,
        conn: sqlite3.Connection,
        records: dict[int, list[_MylarIssueRecord]],
    ) -> None:
        if not self._table_has_columns(
            conn,
            "annuals",
            {
                "IssueID",
                "ComicID",
                "Issue_Number",
                "IssueName",
                "IssueDate",
                "Location",
                "ReleaseComicID",
            },
        ):
            return
        annual_columns = self._table_columns(conn, "annuals")
        query = (
            "SELECT IssueID, ComicID, Issue_Number, IssueName, IssueDate, Location, "
            "ReleaseComicID, ReleaseComicName FROM annuals"
            if "ReleaseComicName" in annual_columns
            else "SELECT IssueID, ComicID, Issue_Number, IssueName, IssueDate, Location, "
            "ReleaseComicID, NULL AS ReleaseComicName FROM annuals"
        )
        for row in conn.execute(query).fetchall():
            owning_comic_id = self._parse_cv_id(row["ComicID"])
            issue_id = self._parse_positive_int(row["IssueID"])
            release_comic_id = self._parse_cv_id(row["ReleaseComicID"])
            if owning_comic_id is None or issue_id is None:
                continue
            records.setdefault(owning_comic_id, []).append(
                _MylarIssueRecord(
                    issue_id=issue_id,
                    series_cv_id=release_comic_id or owning_comic_id,
                    issue_number=row["Issue_Number"],
                    title=row["IssueName"],
                    release_date=row["IssueDate"],
                    location=row["Location"],
                    issue_type=IssueType.ANNUAL,
                    series_name=row["ReleaseComicName"],
                )
            )

    def _table_has_columns(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        required_columns: set[str],
    ) -> bool:
        return required_columns.issubset(self._table_columns(conn, table_name))

    def _table_columns(self, conn: sqlite3.Connection, table_name: str) -> set[str]:
        if table_name == "issues":
            rows = conn.execute("PRAGMA table_info(issues)").fetchall()
        elif table_name == "annuals":
            rows = conn.execute("PRAGMA table_info(annuals)").fetchall()
        else:
            return set()
        return {str(row["name"]) for row in rows}

    def _get_resolved_file_count(self, resolved_path: Path | None) -> int:
        """Count comic files in one validated resolved directory."""
        if resolved_path is None:
            return 0
        return sum(1 for f in resolved_path.iterdir() if f.suffix.lower() in COMIC_EXTENSIONS)
