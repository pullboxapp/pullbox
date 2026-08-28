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

import structlog

from pullbox.core.collection_scanner import COMIC_EXTENSIONS, DiscoveredFile, DiscoveredSeries
from pullbox.core.exceptions import MylarReadError
from pullbox.core.naming import parse_filename
from pullbox.core.release_parser import normalize_issue_number
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
    ) -> None:
        self._db_path = Path(db_path)
        self._path_map = path_map or {}

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
            file_count = self._get_file_count(location)
            has_files = file_count > 0
            series_issue_records = issue_records.get(cv_id, []) if cv_id is not None else []

            # Build sample paths and DiscoveredFile objects from the location directory
            sample_paths: list[str] = []
            discovered_files: list[DiscoveredFile] = []
            resolved_location = self._resolve_location(location)
            if resolved_location:
                p = Path(resolved_location)
                if p.is_dir():
                    comic_paths = sorted(
                        f for f in p.iterdir() if f.suffix.lower() in COMIC_EXTENSIONS
                    )
                    sample_paths = [str(f) for f in comic_paths[:5]]
                    discovered_files = self._build_files(
                        comic_paths,
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
        if not location:
            return None
        resolved = location
        for container_prefix, host_prefix in self._path_map.items():
            if resolved.startswith(container_prefix):
                resolved = host_prefix + resolved[len(container_prefix) :]
                break
        return resolved

    def _build_files(
        self,
        comic_paths: list[Path],
        *,
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
            parsed_series: str | None = None
            parsed_issue_number: float | None = None
            parsed_year: int | None = None
            issue_type = IssueType.ISSUE
            issue_number_raw: str | None = None
            if parsed:
                parsed_series = parsed.series
                parsed_issue_number = parsed.issue_number
                parsed_year = parsed.year
                try:
                    issue_type = IssueType(parsed.issue_type)
                except ValueError:
                    issue_type = IssueType.ISSUE
                if parsed.issue_number == int(parsed.issue_number):
                    issue_number_raw = str(int(parsed.issue_number))
                else:
                    issue_number_raw = str(parsed.issue_number)

            if issue_record is not None and issue_record.issue_number:
                issue_number_raw = issue_record.issue_number
                normalized_issue_number = normalize_issue_number(issue_record.issue_number)
                if normalized_issue_number is not None:
                    parsed_issue_number = normalized_issue_number

            metadata_signals: dict[str, str] = {}
            metadata_diagnostics: dict[str, object] = {}
            comicvine_issue_id: int | None = None
            comicvine_series_id: int | None = None
            if issue_record is not None:
                if issue_record.issue_type != IssueType.ISSUE:
                    issue_type = issue_record.issue_type
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

            results.append(
                DiscoveredFile(
                    file_path=str(fpath),
                    file_name=file_name,
                    file_size=file_size,
                    file_format=file_format,
                    parsed_series=parsed_series,
                    parsed_issue_number=parsed_issue_number,
                    parsed_year=parsed_year,
                    parsed_publisher=None,
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

    def _get_file_count(self, location: str | None) -> int:
        """Count comic files in a directory if it exists. Returns 0 otherwise."""
        resolved = self._resolve_location(location)
        if not resolved:
            return 0
        p = Path(resolved)
        if not p.exists() or not p.is_dir():
            return 0
        return sum(1 for f in p.iterdir() if f.suffix.lower() in COMIC_EXTENSIONS)
