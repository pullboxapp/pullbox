"""Mylar3 database reader for collection import.

Reads series data from a Mylar3 SQLite database (mylar.db) and converts
records to DiscoveredSeries instances, preserving ComicVine IDs for
high-confidence matching during the import identification phase.
"""

from __future__ import annotations

import asyncio
import configparser
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import structlog

from pullbox.core.collection_scanner import COMIC_EXTENSIONS, DiscoveredFile, DiscoveredSeries
from pullbox.core.exceptions import ConfigurationError, MylarReadError
from pullbox.core.issue_numbers import format_issue_number
from pullbox.core.library_file_ownership import build_file_identity_signature
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
from pullbox.core.source_metadata import MetadataSignal, SourceMetadataExtractor
from pullbox.models.issue import IssueType

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Mylar3StoryArcEntrySnapshot:
    """Immutable source evidence for one Mylar story-arc row."""

    ordinal: int
    reading_order: int | None
    reading_order_raw: str | None
    story_arc_id: str | None
    story_arc_name: str | None
    cv_arc_id: str | None
    issue_arc_id: str | None
    issue_id: str | None
    comic_id: str | None
    issue_number: str | None
    comic_name: str | None
    series_year: str | None
    issue_year: str | None
    status: str | None
    location: str | None
    release_date: str | None
    issue_date: str | None
    publisher: str | None
    issue_publisher: str | None
    issue_name: str | None
    manual: str | None
    date_added: str | None
    digital_date: str | None
    issue_type: str | None
    aliases: str | None
    total_issues: str | None
    in_cache_dir: str | None
    int_issue_number: str | None
    dynamic_comic_name: str | None
    volume: str | None
    arc_image: str | None


@dataclass(frozen=True, slots=True)
class Mylar3StoryArcSnapshot:
    """Immutable normalized grouping of Mylar story-arc rows."""

    story_arc_id: str | None
    cv_arc_id: str | None
    name: str | None
    entries: tuple[Mylar3StoryArcEntrySnapshot, ...]


@dataclass(frozen=True, slots=True)
class Mylar3ArcSettingValue:
    """One allowlisted Mylar arc setting and its reviewable raw value."""

    key: str
    section: str
    value: bool | str | None
    raw_value: str | None
    used_default: bool


@dataclass(frozen=True, slots=True)
class Mylar3ArcSettingsSnapshot:
    """Bounded, secret-free snapshot of Mylar's story-arc settings."""

    present: bool
    parse_warnings: tuple[str, ...]
    values: tuple[Mylar3ArcSettingValue, ...]


@dataclass(frozen=True, slots=True)
class Mylar3CollectionSnapshot:
    """One read-only snapshot of Mylar series, arcs, and read-list inventory."""

    series: tuple[DiscoveredSeries, ...]
    story_arcs: tuple[Mylar3StoryArcSnapshot, ...]
    storyarcs_present: bool
    readlist_present: bool
    readlist_count: int
    arc_settings: Mylar3ArcSettingsSnapshot


@dataclass(frozen=True, slots=True)
class Mylar3ImportMetadataSnapshot:
    """Small source-wide metadata shared by bounded Mylar import pages."""

    storyarcs_present: bool
    readlist_present: bool
    readlist_count: int
    arc_settings: Mylar3ArcSettingsSnapshot
    series_count: int = 0


@dataclass(frozen=True, slots=True)
class Mylar3StoryArcPreflightExample:
    """One path-free Story Arc row suitable for a bounded Step 1 preview."""

    story_arc: str | None
    series: str | None
    issue_number: str | None
    issue_title: str | None
    reading_order: str | None
    status: str | None


@dataclass(frozen=True, slots=True)
class Mylar3StoryArcPreflightSnapshot:
    """Exact aggregate counts plus bounded examples from Mylar Story Arc tables."""

    storyarcs_present: bool
    arcs_count: int
    entries_count: int
    missing_count: int
    duplicate_count: int
    existing_location_count: int
    examples: tuple[Mylar3StoryArcPreflightExample, ...]
    readlist_present: bool
    readlist_count: int
    arc_settings: Mylar3ArcSettingsSnapshot
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _MylarArcSettingSpec:
    """Static schema for one safe config value."""

    key: str
    section: str
    kind: str
    default: bool | str | None


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
        config_path: Optional explicit Mylar config.ini path. When omitted, a
                     safe sibling config.ini is discovered without following links.
        include_missing_files: Retain full recorded issue paths, including missing
                               files, for in-place source eligibility review.
        reference_root_boundaries: Frozen lexical/resolved root pairs used by
                                   in-place scans to mirror confirmed path semantics.
    """

    MYLAR3_CV_PREFIX = "CV-"
    MAX_CONFIG_BYTES = 1_048_576
    DEFAULT_IMPORT_PAGE_SIZE = 100
    MAX_IMPORT_PAGE_SIZE = 250
    ARC_SETTING_SPECS = (
        _MylarArcSettingSpec("STORYARCDIR", "StoryArc", "bool", False),
        _MylarArcSettingSpec("STORYARC_LOCATION", "StoryArc", "str", None),
        _MylarArcSettingSpec("COPY2ARCDIR", "StoryArc", "bool", False),
        _MylarArcSettingSpec(
            "ARC_FOLDERFORMAT",
            "StoryArc",
            "str",
            "$arc ($spanyears)",
        ),
        _MylarArcSettingSpec("ARC_FILEOPS", "StoryArc", "str", "copy"),
        _MylarArcSettingSpec(
            "ARC_FILEOPS_SOFTLINK_RELATIVE",
            "StoryArc",
            "bool",
            False,
        ),
        _MylarArcSettingSpec("UPCOMING_STORYARCS", "StoryArc", "bool", False),
        _MylarArcSettingSpec("SEARCH_STORYARCS", "StoryArc", "bool", False),
        _MylarArcSettingSpec("READ2FILENAME", "General", "bool", False),
    )
    STORY_ARC_COLUMNS = (
        "StoryArcID",
        "ComicName",
        "IssueNumber",
        "SeriesYear",
        "IssueYEAR",
        "StoryArc",
        "TotalIssues",
        "Status",
        "inCacheDir",
        "Location",
        "IssueArcID",
        "ReadingOrder",
        "IssueID",
        "ComicID",
        "ReleaseDate",
        "IssueDate",
        "Publisher",
        "IssuePublisher",
        "IssueName",
        "CV_ArcID",
        "Int_IssueNumber",
        "DynamicComicName",
        "Volume",
        "Manual",
        "DateAdded",
        "DigitalDate",
        "Type",
        "Aliases",
        "ArcImage",
    )

    def __init__(
        self,
        db_path: str | Path,
        path_map: dict[str, str] | None = None,
        source_layout: SourceLayoutSpec | None = None,
        config_path: str | Path | None = None,
        *,
        include_missing_files: bool = False,
        reference_root_boundaries: Sequence[tuple[Path, Path]] | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._config_path = Path(config_path) if config_path is not None else None
        self._path_map = path_map or {}
        self._include_missing_files = include_missing_files
        self.import_series_rows_read = 0
        self._reference_root_boundaries = (
            None
            if reference_root_boundaries is None
            else tuple(
                (Path(lexical).expanduser().absolute(), Path(resolved))
                for lexical, resolved in reference_root_boundaries
            )
        )
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
        snapshot = await self.read_snapshot()
        return list(snapshot.series)

    async def read_collection(self) -> Mylar3CollectionSnapshot:
        """Read the complete normalized Mylar source collection."""
        return await self.read_snapshot()

    async def read_snapshot(self) -> Mylar3CollectionSnapshot:
        """Read series, story arcs, and the read-list count in one DB snapshot."""
        if not self._db_path.exists():
            msg = f"Mylar3 database not found: {self._db_path}"
            raise FileNotFoundError(msg)

        return await asyncio.to_thread(self._read_snapshot_sync)

    async def read_import_metadata(self) -> Mylar3ImportMetadataSnapshot:
        """Read source-wide flags without retaining comics, issues, or arc rows."""
        self._require_database()
        return await asyncio.to_thread(self._read_import_metadata_sync)

    async def iter_import_series_pages(
        self,
        *,
        page_size: int = DEFAULT_IMPORT_PAGE_SIZE,
    ) -> AsyncIterator[tuple[DiscoveredSeries, ...]]:
        """Yield complete normalized series cohorts from bounded Comic row pages."""
        self._require_database()
        self._require_import_page_size(page_size)
        after_rowid = 0
        self.import_series_rows_read = 0
        seen_cv_ids: set[int] = set()
        while True:
            page, next_rowid, source_count = await asyncio.to_thread(
                self._read_import_series_page_sync,
                after_rowid,
                page_size,
            )
            if next_rowid == after_rowid:
                break
            after_rowid = next_rowid
            self.import_series_rows_read += source_count
            filtered: list[DiscoveredSeries] = []
            for series in page:
                cv_id = series.mylar3_cv_id
                if cv_id is not None and cv_id in seen_cv_ids:
                    logger.warning(
                        "mylar3_duplicate_cv_id",
                        cv_id=cv_id,
                        series_name=series.raw_series_name,
                    )
                    continue
                if cv_id is not None:
                    seen_cv_ids.add(cv_id)
                filtered.append(series)
            if filtered:
                yield tuple(filtered)

    async def iter_import_story_arc_pages(
        self,
        *,
        page_size: int = DEFAULT_IMPORT_PAGE_SIZE,
    ) -> AsyncIterator[tuple[Mylar3StoryArcSnapshot, ...]]:
        """Yield deterministic pages of complete Story Arc cohorts."""
        self._require_database()
        self._require_import_page_size(page_size)
        after_group: tuple[str, str] | None = None
        while True:
            page, after_group = await asyncio.to_thread(
                self._read_import_story_arc_page_sync,
                after_group,
                page_size,
            )
            if not page:
                break
            yield page

    async def read_story_arc_preflight(
        self,
        *,
        max_examples: int = 5,
    ) -> Mylar3StoryArcPreflightSnapshot:
        """Read aggregate Story Arc evidence without loading the Mylar collection."""
        if max_examples < 1 or max_examples > 20:
            raise ValueError("Mylar Story Arc preview examples must be between 1 and 20")
        if not self._db_path.exists():
            msg = f"Mylar3 database not found: {self._db_path}"
            raise FileNotFoundError(msg)
        return await asyncio.to_thread(self._read_story_arc_preflight_sync, max_examples)

    def _read_sync(self) -> list[DiscoveredSeries]:
        """Retain the legacy synchronous series-only helper."""
        return list(self._read_snapshot_sync().series)

    def _read_snapshot_sync(self) -> Mylar3CollectionSnapshot:
        """Read every supported Mylar source domain through one connection."""
        try:
            conn = sqlite3.connect(f"{self._db_path.resolve().as_uri()}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.DatabaseError as exc:
            msg = f"Could not read Mylar3 database: {exc}"
            raise MylarReadError(msg) from exc

        try:
            conn.execute("BEGIN")
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
            story_arc_rows, storyarcs_present = self._read_story_arc_rows(conn)
            readlist_present, readlist_count = self._read_readlist_count(conn)
        except sqlite3.DatabaseError as exc:
            msg = f"Could not read Mylar3 database: {exc}"
            raise MylarReadError(msg) from exc
        finally:
            conn.close()

        return Mylar3CollectionSnapshot(
            series=tuple(self._convert_rows(rows, issue_records)),
            story_arcs=self._convert_story_arc_rows(story_arc_rows),
            storyarcs_present=storyarcs_present,
            readlist_present=readlist_present,
            readlist_count=readlist_count,
            arc_settings=self._read_arc_settings(),
        )

    def _require_database(self) -> None:
        if not self._db_path.exists():
            msg = f"Mylar3 database not found: {self._db_path}"
            raise FileNotFoundError(msg)

    def _require_import_page_size(self, page_size: int) -> None:
        if page_size < 1 or page_size > self.MAX_IMPORT_PAGE_SIZE:
            msg = f"Mylar import page size must be between 1 and {self.MAX_IMPORT_PAGE_SIZE}"
            raise ValueError(msg)

    def _read_import_metadata_sync(self) -> Mylar3ImportMetadataSnapshot:
        """Read bounded source-wide inventory in one explicit read transaction."""
        try:
            conn = sqlite3.connect(f"{self._db_path.resolve().as_uri()}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.DatabaseError as exc:
            msg = f"Could not read Mylar3 database: {exc}"
            raise MylarReadError(msg) from exc

        try:
            conn.execute("BEGIN")
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='comics'"
            ).fetchone():
                msg = "Not a Mylar3 database: 'comics' table not found"
                raise MylarReadError(msg)
            storyarcs_present = self._table_exists(conn, "storyarcs")
            readlist_present, readlist_count = self._read_readlist_count(conn)
            series_count = int(conn.execute("SELECT COUNT(*) FROM comics").fetchone()[0])
        except sqlite3.DatabaseError as exc:
            msg = f"Could not read Mylar3 database: {exc}"
            raise MylarReadError(msg) from exc
        finally:
            conn.close()

        return Mylar3ImportMetadataSnapshot(
            storyarcs_present=storyarcs_present,
            readlist_present=readlist_present,
            readlist_count=readlist_count,
            arc_settings=self._read_arc_settings(),
            series_count=series_count,
        )

    def _read_import_series_page_sync(
        self,
        after_rowid: int,
        page_size: int,
    ) -> tuple[tuple[DiscoveredSeries, ...], int, int]:
        """Read and normalize one bounded rowid-keyset Comic cohort."""
        try:
            conn = sqlite3.connect(f"{self._db_path.resolve().as_uri()}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.DatabaseError as exc:
            msg = f"Could not read Mylar3 database: {exc}"
            raise MylarReadError(msg) from exc

        try:
            conn.execute("BEGIN")
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='comics'"
            ).fetchone():
                msg = "Not a Mylar3 database: 'comics' table not found"
                raise MylarReadError(msg)
            cursor = conn.execute(
                "SELECT ComicID, ComicName, ComicYear, ComicPublisher, "
                "ComicLocation, Status, Total, rowid AS __source_rowid "
                "FROM comics WHERE rowid > ? ORDER BY rowid LIMIT ?",
                (after_rowid, page_size),
            )
            rows = list(cursor)
            if not rows:
                return (), after_rowid, 0
            comic_ids = {
                cv_id for row in rows if (cv_id := self._parse_cv_id(row["ComicID"])) is not None
            }
            issue_records = self._read_issue_records_for_comic_ids(conn, comic_ids)
            release_ids = {
                record.series_cv_id
                for owning_comic_id, records in issue_records.items()
                for record in records
                if record.issue_type == IssueType.ANNUAL and record.series_cv_id != owning_comic_id
            }
            normal_release_ids = self._existing_comic_ids(conn, release_ids)
            converted = self._convert_rows(rows, issue_records)
            page_items = [
                series
                for series in converted
                if not (
                    series.mylar3_cv_id in normal_release_ids
                    and series.mylar3_cv_id not in comic_ids
                )
            ]
            cross_release_records = self._read_cross_release_records(conn, comic_ids)
            if cross_release_records:
                owner_rows = self._read_comic_rows_by_cv_ids(
                    conn,
                    {owner_cv_id for owner_cv_id, _record in cross_release_records},
                )
                self._attach_cross_release_records(
                    page_items,
                    cross_release_records=cross_release_records,
                    owner_rows=owner_rows,
                )
            page = tuple(page_items)
            next_rowid = int(rows[-1]["__source_rowid"])
        except sqlite3.DatabaseError as exc:
            msg = f"Could not read Mylar3 database: {exc}"
            raise MylarReadError(msg) from exc
        finally:
            conn.close()
        return page, next_rowid, len(rows)

    def _read_import_story_arc_page_sync(
        self,
        after_group: tuple[str, str] | None,
        page_size: int,
    ) -> tuple[tuple[Mylar3StoryArcSnapshot, ...], tuple[str, str] | None]:
        """Read one keyset page of complete Story Arc identity cohorts."""
        try:
            conn = sqlite3.connect(f"{self._db_path.resolve().as_uri()}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.DatabaseError as exc:
            msg = f"Could not read Mylar3 database: {exc}"
            raise MylarReadError(msg) from exc

        try:
            conn.execute("BEGIN")
            if not self._table_exists(conn, "storyarcs"):
                return (), after_group
            available_columns = {
                column.casefold(): column for column in self._table_columns(conn, "storyarcs")
            }

            def expression(column_name: str) -> str:
                actual_column = available_columns.get(column_name.casefold())
                if actual_column is None:
                    return "NULL"
                return f'"{actual_column.replace(chr(34), chr(34) * 2)}"'

            story_arc_id = f"COALESCE(CAST({expression('StoryArcID')} AS TEXT), '')"
            cv_arc_id = f"COALESCE(CAST({expression('CV_ArcID')} AS TEXT), '')"
            story_arc_name = f"COALESCE(CAST({expression('StoryArc')} AS TEXT), '')"
            group_kind = (
                f"CASE WHEN {story_arc_id} <> '' THEN 'story_arc_id' "
                f"WHEN {cv_arc_id} <> '' THEN 'cv_arc_id' "
                f"WHEN {story_arc_name} <> '' THEN 'story_arc_name' "
                "ELSE 'unidentified_row' END"
            )
            group_value = (
                f"CASE WHEN {story_arc_id} <> '' THEN {story_arc_id} "
                f"WHEN {cv_arc_id} <> '' THEN {cv_arc_id} "
                f"WHEN {story_arc_name} <> '' THEN {story_arc_name} "
                "ELSE printf('%020d', rowid) END"
            )
            key_params: list[object] = []
            key_where = ""
            if after_group is not None:
                key_where = "WHERE group_kind > ? OR (group_kind = ? AND group_value > ?)"
                key_params.extend([after_group[0], after_group[0], after_group[1]])
            key_params.append(page_size)
            key_query = (
                "SELECT group_kind, group_value FROM ("
                f"SELECT {group_kind} AS group_kind, {group_value} AS group_value "
                "FROM storyarcs"
                ") AS normalized "
                f"{key_where} "
                "GROUP BY group_kind, group_value "
                "ORDER BY group_kind, group_value LIMIT ?"
            )
            key_cursor = conn.execute(
                key_query,
                tuple(key_params),
            )
            group_rows = list(key_cursor)
            if not group_rows:
                return (), after_group
            group_keys = [(str(row["group_kind"]), str(row["group_value"])) for row in group_rows]

            expressions: list[str] = []
            for expected_column in self.STORY_ARC_COLUMNS:
                actual_column = available_columns.get(expected_column.casefold())
                if actual_column is None:
                    expressions.append(f'NULL AS "{expected_column}"')
                    continue
                quoted_column = actual_column.replace('"', '""')
                expressions.append(f'"{quoted_column}" AS "{expected_column}"')
            predicates = " OR ".join(
                f"({group_kind} = ? AND {group_value} = ?)" for _key in group_keys
            )
            row_params = tuple(value for key in group_keys for value in key)
            row_query = (
                f"SELECT {', '.join(expressions)}, rowid AS __source_rowid "
                f"FROM storyarcs WHERE {predicates} "
                f"ORDER BY {group_kind}, {group_value}, rowid"
            )
            row_cursor = conn.execute(row_query, row_params)
            rows = list(row_cursor)
            page = self._convert_story_arc_rows(rows)
            next_group = group_keys[-1]
        except sqlite3.DatabaseError as exc:
            msg = f"Could not read Mylar3 database: {exc}"
            raise MylarReadError(msg) from exc
        finally:
            conn.close()
        return page, next_group

    def _read_story_arc_preflight_sync(
        self,
        max_examples: int,
    ) -> Mylar3StoryArcPreflightSnapshot:
        """Read exact aggregate counts and a bounded row sample in one RO transaction."""
        try:
            conn = sqlite3.connect(f"{self._db_path.resolve().as_uri()}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.DatabaseError as exc:
            msg = f"Could not read Mylar3 database: {exc}"
            raise MylarReadError(msg) from exc

        warnings: list[str] = []
        try:
            conn.execute("BEGIN")
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='comics'"
            )
            if not cursor.fetchone():
                msg = "Not a Mylar3 database: 'comics' table not found"
                raise MylarReadError(msg)
            if not self._table_exists(conn, "storyarcs"):
                readlist_present, readlist_count = self._read_readlist_count(conn)
                return Mylar3StoryArcPreflightSnapshot(
                    storyarcs_present=False,
                    arcs_count=0,
                    entries_count=0,
                    missing_count=0,
                    duplicate_count=0,
                    existing_location_count=0,
                    examples=(),
                    readlist_present=readlist_present,
                    readlist_count=readlist_count,
                    arc_settings=self._read_arc_settings(),
                )

            available = {
                column.casefold(): column for column in self._table_columns(conn, "storyarcs")
            }

            def expression(column_name: str) -> str:
                actual = available.get(column_name.casefold())
                if actual is None:
                    return "NULL"
                return f'"{actual.replace(chr(34), chr(34) * 2)}"'

            story_arc_id = expression("StoryArcID")
            cv_arc_id = expression("CV_ArcID")
            story_arc_name = expression("StoryArc")
            reading_order = expression("ReadingOrder")
            status = expression("Status")
            location = expression("Location")
            story_arc_id_text = f"COALESCE(CAST({story_arc_id} AS TEXT), '')"
            cv_arc_id_text = f"COALESCE(CAST({cv_arc_id} AS TEXT), '')"
            story_arc_name_text = f"COALESCE(CAST({story_arc_name} AS TEXT), '')"
            identity_predicate = (
                f"({story_arc_id_text} <> '' OR {cv_arc_id_text} <> '' "
                f"OR {story_arc_name_text} <> '')"
            )
            group_kind = (
                f"CASE WHEN {story_arc_id_text} <> '' THEN 'story_arc_id' "
                f"WHEN {cv_arc_id_text} <> '' THEN 'cv_arc_id' "
                "ELSE 'story_arc_name' END"
            )
            group_value = (
                f"CASE WHEN {story_arc_id_text} <> '' THEN {story_arc_id_text} "
                f"WHEN {cv_arc_id_text} <> '' THEN {cv_arc_id_text} "
                f"ELSE {story_arc_name_text} END"
            )
            has_identity_columns = any(
                available.get(column.casefold())
                for column in ("StoryArcID", "CV_ArcID", "StoryArc")
            )

            entries_count = int(
                conn.execute("SELECT COUNT(*) AS count_value FROM storyarcs").fetchone()[
                    "count_value"
                ]
            )
            identified_arcs_query = (
                "SELECT COUNT(*) AS count_value FROM ("
                "SELECT 1 FROM storyarcs WHERE "
                f"{identity_predicate} GROUP BY {group_kind}, {group_value})"
            )
            identified_arcs_count = int(
                conn.execute(identified_arcs_query).fetchone()["count_value"]
            )
            unidentified_arcs_query = (
                f"SELECT COUNT(*) AS count_value FROM storyarcs WHERE NOT {identity_predicate}"
            )
            unidentified_arcs_count = int(
                conn.execute(unidentified_arcs_query).fetchone()["count_value"]
            )
            arcs_count = identified_arcs_count + unidentified_arcs_count
            missing_query = (
                "SELECT COUNT(*) AS count_value FROM storyarcs WHERE "
                f"LOWER(TRIM(COALESCE(CAST({status} AS TEXT), ''))) IN ('missing', 'wanted')"
            )
            missing_count = int(conn.execute(missing_query).fetchone()["count_value"])
            existing_location_query = (
                "SELECT COUNT(*) AS count_value FROM storyarcs WHERE "
                f"TRIM(COALESCE(CAST({location} AS TEXT), '')) <> ''"
            )
            existing_location_count = int(
                conn.execute(existing_location_query).fetchone()["count_value"]
            )
            duplicate_query = (
                "SELECT COALESCE(SUM(group_count - 1), 0) AS count_value FROM ("
                "SELECT COUNT(*) AS group_count FROM storyarcs WHERE "
                f"{identity_predicate} AND "
                f"TRIM(COALESCE(CAST({reading_order} AS TEXT), '')) <> '' "
                f"GROUP BY {group_kind}, {group_value}, {reading_order} "
                "HAVING COUNT(*) > 1)"
            )
            duplicate_count = int(conn.execute(duplicate_query).fetchone()["count_value"])

            selected = {
                "story_arc": story_arc_name,
                "series": expression("ComicName"),
                "issue_number": expression("IssueNumber"),
                "issue_title": expression("IssueName"),
                "reading_order": reading_order,
                "status": status,
            }
            select_list = ", ".join(f'{column} AS "{alias}"' for alias, column in selected.items())
            examples_query = (
                f"SELECT {select_list} FROM storyarcs "
                f"ORDER BY {story_arc_name}, {reading_order} LIMIT ?"
            )
            rows = conn.execute(examples_query, (max_examples,)).fetchall()
            examples = tuple(
                Mylar3StoryArcPreflightExample(
                    story_arc=self._bounded_preflight_text(row["story_arc"]),
                    series=self._bounded_preflight_text(row["series"]),
                    issue_number=self._bounded_preflight_text(row["issue_number"]),
                    issue_title=self._bounded_preflight_text(row["issue_title"]),
                    reading_order=self._bounded_preflight_text(row["reading_order"]),
                    status=self._bounded_preflight_text(row["status"]),
                )
                for row in rows
            )
            readlist_present, readlist_count = self._read_readlist_count(conn)
        except sqlite3.DatabaseError as exc:
            msg = f"Could not read Mylar3 database: {exc}"
            raise MylarReadError(msg) from exc
        finally:
            conn.close()

        if not has_identity_columns:
            warnings.append("story_arc_identity_columns_missing")
        return Mylar3StoryArcPreflightSnapshot(
            storyarcs_present=True,
            arcs_count=arcs_count,
            entries_count=entries_count,
            missing_count=missing_count,
            duplicate_count=duplicate_count,
            existing_location_count=existing_location_count,
            examples=examples,
            readlist_present=readlist_present,
            readlist_count=readlist_count,
            arc_settings=self._read_arc_settings(),
            warnings=tuple(warnings),
        )

    def _bounded_preflight_text(self, value: object, *, max_length: int = 200) -> str | None:
        """Return one single-line display value without retaining paths or control text."""
        if value is None:
            return None
        text = " ".join(str(value).split()).strip()
        return text[:max_length] or None

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
            comic_paths: list[Path] = []
            if resolved_location and Path(resolved_location).is_dir():
                comic_paths = sorted(
                    f
                    for f in Path(resolved_location).iterdir()
                    if f.suffix.lower() in COMIC_EXTENSIONS
                )
            comic_paths = self._include_recorded_issue_paths(
                comic_paths,
                path_resolution,
                series_issue_records,
            )
            if comic_paths:
                sample_paths = [str(f) for f in comic_paths[:5]]
                discovered_files = self._build_files(
                    comic_paths,
                    source_location=location,
                    series_cv_id=cv_id,
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

            recorded_path_resolutions = [
                resolution
                for record in series_issue_records
                if record.location
                and (
                    resolution := self._selected_recorded_issue_path_details(
                        record.location,
                        path_resolution,
                    )
                )
                is not None
                and resolution.path is not None
                and resolution.path in comic_paths
            ]
            recorded_paths_replace_series_location = bool(
                path_resolution.path is None
                and path_resolution.status in {"missing", "unmapped"}
                and discovered_files
                and recorded_path_resolutions
            )
            diagnostics: dict[str, object] = {}
            if recorded_paths_replace_series_location:
                recorded_mapping_applied = any(
                    resolution.mapping_applied for resolution in recorded_path_resolutions
                )
                diagnostics["mylar3_path"] = {
                    "status": "mapped" if recorded_mapping_applied else "local",
                    "mapping_applied": recorded_mapping_applied,
                }
                diagnostics["mylar3_series_location"] = {
                    "status": path_resolution.status,
                    "mapping_applied": path_resolution.mapping_applied,
                    "reason": path_resolution.reason,
                }
            else:
                diagnostics["mylar3_path"] = {
                    "status": path_resolution.status,
                    "mapping_applied": path_resolution.mapping_applied,
                }
            if path_resolution.reason is not None and not recorded_paths_replace_series_location:
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
        if ".." in location_path.parts or self._path_text_is_invalid(location):
            return _ResolvedMylarPath(
                path=None,
                status="invalid",
                mapping_applied=bool(self._path_map),
                reason="unsafe_path_mapping",
                rejection_reason=(
                    "The Mylar comic folder resolves outside the configured mapped root."
                ),
            )

        try:
            resolved_local_path = location_path.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            resolved_local_path = None
        identity_outside_root = False
        if resolved_local_path is not None and resolved_local_path.is_dir():
            identity_status = self._root_claim_status(location_path, resolved_local_path)
            if identity_status == "allowed":
                return _ResolvedMylarPath(
                    path=location_path if self._include_missing_files else resolved_local_path,
                    status="local",
                    mapping_applied=False,
                )
            if identity_status == "ambiguous":
                return _ResolvedMylarPath(
                    path=location_path if self._include_missing_files else None,
                    status="ambiguous",
                    mapping_applied=False,
                    reason="source_root_ambiguous",
                    rejection_reason=(
                        "The Mylar comic folder matches ambiguous reference-capable roots."
                    ),
                )
            identity_outside_root = True

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
            mapped_identity_status = self._root_claim_status(host_root / relative, resolved_path)
            if mapped_identity_status == "ambiguous":
                return _ResolvedMylarPath(
                    path=host_root / relative if self._include_missing_files else None,
                    status="ambiguous",
                    mapping_applied=True,
                    reason="source_root_ambiguous",
                    rejection_reason=(
                        "The mapped Mylar comic folder matches ambiguous reference-capable roots."
                    ),
                )
            if mapped_identity_status == "outside":
                return _ResolvedMylarPath(
                    path=host_root / relative if self._include_missing_files else None,
                    status="outside_root",
                    mapping_applied=True,
                    reason="source_outside_root",
                    rejection_reason=(
                        "The mapped Mylar comic folder is outside every enabled "
                        "reference-capable root."
                    ),
                )
            return _ResolvedMylarPath(
                path=host_root / relative if self._include_missing_files else resolved_path,
                status="mapped",
                mapping_applied=True,
            )

        if identity_outside_root:
            return _ResolvedMylarPath(
                path=location_path if self._include_missing_files else None,
                status="outside_root",
                mapping_applied=False,
                reason="source_outside_root",
                rejection_reason=(
                    "The Mylar comic folder is outside every enabled reference-capable root."
                ),
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

    def _root_claim_status(self, lexical_path: Path, resolved_path: Path) -> str:
        """Match preflight's lexical-and-resolved root containment contract."""
        if self._reference_root_boundaries is None:
            return "allowed"
        try:
            lexical = lexical_path.expanduser().absolute()
        except (OSError, RuntimeError, ValueError):
            return "outside"
        claims = sum(
            lexical.is_relative_to(root_lexical) and resolved_path.is_relative_to(root_resolved)
            for root_lexical, root_resolved in self._reference_root_boundaries
        )
        if claims == 1:
            return "allowed"
        return "ambiguous" if claims > 1 else "outside"

    @staticmethod
    def _path_text_is_invalid(value: str) -> bool:
        return len(value) > 4096 or not value.isprintable()

    def _build_files(
        self,
        comic_paths: list[Path],
        *,
        source_location: str | None,
        series_cv_id: int | None,
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
        series_resolution = self._resolve_location_details(source_location)
        issue_by_path = self._issue_records_by_path(issue_records, series_resolution)
        reconciled = self._reconcile_missing_recorded_paths(comic_paths, issue_by_path)
        for actual, recorded in reconciled.items():
            issue_by_path[str(actual)] = issue_by_path[str(recorded)]
        replaced_paths = set(reconciled.values())
        comic_paths = [path for path in comic_paths if path not in replaced_paths]
        extractor = SourceMetadataExtractor()
        sidecars_by_folder = {
            folder: extractor.read_sidecars(folder)
            for folder in dict.fromkeys(path.parent for path in comic_paths)
        }
        for fpath in comic_paths:
            sidecar_data = sidecars_by_folder[fpath.parent]
            file_name = fpath.name
            file_format = fpath.suffix.lstrip(".").lower()
            issue_record = issue_by_path.get(str(fpath)) or issue_by_file_name.get(
                file_name.casefold()
            )
            try:
                source_signature = build_file_identity_signature(fpath)
                file_size = int(source_signature["size"])
            except (OSError, RuntimeError, ValueError, ConfigurationError):
                source_signature = {}
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

            normalized_sidecar = sidecar_data or {}
            metadata_diagnostics: dict[str, object] = {
                "sidecar_files_present": sorted(
                    str(name) for name in normalized_sidecar.get("files_present") or []
                ),
                "archive_metadata_loaded": False,
                "archive_metadata_deferred": True,
                "has_comicinfo": False,
                "mylar3_folder_metadata_scanned": True,
            }
            if fpath in reconciled:
                metadata_diagnostics["mylar3_path_reconciliation"] = {
                    "recorded_path": str(reconciled[fpath]),
                    "actual_path": str(fpath),
                    "method": "unique_same_folder_normalized_stem",
                }
            if sidecar_data is not None and (
                sidecar_data.get("files_present")
                or sidecar_data.get("series_id") is not None
                or sidecar_data.get("issue_id") is not None
                or sidecar_data.get("booktype") is not None
                or sidecar_data.get("series_status") is not None
                or sidecar_data.get("issue_count") is not None
                or sidecar_data.get("series_name") is not None
                or sidecar_data.get("year") is not None
                or sidecar_data.get("identity_conflicts")
            ):
                sidecar_booktype = sidecar_data.get("booktype")
                metadata_diagnostics["sidecar_snapshot"] = {
                    "files_present": list(sidecar_data.get("files_present") or []),
                    "series_id": sidecar_data.get("series_id"),
                    "series_id_source": sidecar_data.get("series_id_source"),
                    "issue_id": sidecar_data.get("issue_id"),
                    "booktype": (
                        sidecar_booktype.value
                        if isinstance(sidecar_booktype, IssueType)
                        else sidecar_booktype
                    ),
                    "series_status": sidecar_data.get("series_status"),
                    "issue_count": sidecar_data.get("issue_count"),
                    "series_name": sidecar_data.get("series_name"),
                    "year": sidecar_data.get("year"),
                    "identity_conflicts": list(sidecar_data.get("identity_conflicts") or []),
                }
            sidecar_identity: dict[str, object] = {}
            sidecar_series_id = normalized_sidecar.get("series_id")
            sidecar_issue_id = normalized_sidecar.get("issue_id")
            if isinstance(sidecar_series_id, int):
                sidecar_identity["comicvine_series_id"] = sidecar_series_id
            else:
                sidecar_series_id = None
            if isinstance(sidecar_issue_id, int):
                sidecar_identity["comicvine_issue_id"] = sidecar_issue_id
            else:
                sidecar_issue_id = None
            if sidecar_identity:
                metadata_diagnostics["sidecar_identity"] = sidecar_identity
            raw_identity_conflicts = normalized_sidecar.get("identity_conflicts")
            identity_conflicts = (
                [
                    dict(conflict)
                    for conflict in raw_identity_conflicts
                    if isinstance(conflict, dict)
                ]
                if isinstance(raw_identity_conflicts, list)
                else []
            )
            if (
                sidecar_series_id is not None
                and series_cv_id is not None
                and sidecar_series_id != series_cv_id
            ):
                identity_conflicts.append(
                    {
                        "field": "comicvine_series_id",
                        "mylar3": series_cv_id,
                        "sidecar": sidecar_series_id,
                    }
                )
            if identity_conflicts:
                metadata_diagnostics["identity_conflicts"] = identity_conflicts

            comicvine_issue_id = sidecar_issue_id
            comicvine_series_id = series_cv_id or sidecar_series_id
            if comicvine_issue_id is not None:
                metadata_signals["comicvine_issue_id"] = MetadataSignal.SIDECAR.value
            if comicvine_series_id is not None:
                metadata_signals["comicvine_series_id"] = (
                    MetadataSignal.MYLAR3.value
                    if series_cv_id is not None
                    else MetadataSignal.SIDECAR.value
                )
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
                    source_signature=source_signature,
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

    @staticmethod
    def _reconcile_missing_recorded_paths(
        comic_paths: list[Path],
        issue_by_path: dict[str, _MylarIssueRecord],
    ) -> dict[Path, Path]:
        """Accept only unambiguous case/spacing/extension drift, never issue-number guesses."""
        existing: dict[tuple[Path, str], list[Path]] = {}
        missing: dict[tuple[Path, str], list[Path]] = {}
        for path in comic_paths:
            normalized = " ".join(path.stem.casefold().split())
            normalized = re.sub(r"\s*([.()\[\]])\s*", r"\1", normalized)
            key = (path.parent, normalized)
            try:
                if path.is_file() and not path.is_symlink():
                    existing.setdefault(key, []).append(path)
                elif not path.exists() and str(path) in issue_by_path:
                    missing.setdefault(key, []).append(path)
            except OSError:
                continue
        reconciled: dict[Path, Path] = {}
        for key, recorded in missing.items():
            candidates = existing.get(key, [])
            if len(recorded) != 1 or len(candidates) != 1:
                continue
            actual = candidates[0]
            if str(actual) in issue_by_path:
                continue
            reconciled[actual] = recorded[0]
        return reconciled

    def _include_recorded_issue_paths(
        self,
        comic_paths: list[Path],
        series_resolution: _ResolvedMylarPath,
        issue_records: list[_MylarIssueRecord],
    ) -> list[Path]:
        """Merge safely resolved records, retaining missing paths only in-place."""
        paths = set(comic_paths)
        for record in issue_records:
            if not record.location:
                continue
            path = Path(record.location)
            if path.suffix.lower() not in COMIC_EXTENSIONS:
                continue
            resolution = self._selected_recorded_issue_path_details(
                record.location,
                series_resolution,
            )
            if resolution is not None and resolution.path is not None:
                paths.add(resolution.path)
        return sorted(paths)

    def _issue_records_by_path(
        self,
        issue_records: list[_MylarIssueRecord],
        series_resolution: _ResolvedMylarPath,
    ) -> dict[str, _MylarIssueRecord]:
        """Index issue identities by their exact selected source path."""
        records: dict[str, _MylarIssueRecord] = {}
        ambiguous: set[str] = set()
        for record in issue_records:
            if not record.location:
                continue
            resolution = self._selected_recorded_issue_path_details(
                record.location,
                series_resolution,
            )
            if resolution is not None and resolution.path is not None:
                path = str(resolution.path)
                existing = records.get(path)
                if existing is not None and existing.issue_id != record.issue_id:
                    ambiguous.add(path)
                records[path] = record
        for path in ambiguous:
            records.pop(path, None)
        return records

    def _selected_recorded_issue_path_details(
        self,
        location: str,
        series_resolution: _ResolvedMylarPath,
    ) -> _ResolvedMylarPath | None:
        """Resolve one exact issue source under the confirmed series/path-map contract."""
        if self._path_text_is_invalid(location):
            return None
        path = Path(location)
        if ".." in path.parts or path.suffix.lower() not in COMIC_EXTENSIONS:
            return None

        if not path.is_absolute():
            if series_resolution.path is None:
                return None
            try:
                resolved_series = series_resolution.path.resolve(strict=False)
                resolved_path = (series_resolution.path / path).resolve(strict=False)
            except (OSError, RuntimeError, ValueError):
                return None
            if not resolved_path.is_relative_to(resolved_series):
                return None
            if not self._include_missing_files and not resolved_path.is_file():
                return None
            return _ResolvedMylarPath(
                path=(series_resolution.path / path)
                if self._include_missing_files
                else resolved_path,
                status=series_resolution.status,
                mapping_applied=series_resolution.mapping_applied,
            )

        try:
            identity_path = path.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            identity_path = None
        identity_exists = identity_path is not None and identity_path.is_file()
        if identity_exists:
            assert identity_path is not None
            identity_status = self._root_claim_status(path, identity_path)
            if identity_status == "allowed":
                return _ResolvedMylarPath(
                    path=path if self._include_missing_files else identity_path,
                    status="local",
                    mapping_applied=False,
                )
            if identity_status == "ambiguous":
                return (
                    _ResolvedMylarPath(
                        path=path,
                        status="ambiguous",
                        mapping_applied=False,
                        reason="source_root_ambiguous",
                    )
                    if self._include_missing_files
                    else None
                )

        for container_prefix, host_prefix in self._ordered_path_map_items():
            container_root = Path(container_prefix)
            try:
                relative = path.relative_to(container_root)
            except ValueError:
                continue
            host_root = Path(host_prefix)
            if (
                not container_root.is_absolute()
                or not host_root.is_absolute()
                or ".." in relative.parts
            ):
                return None
            try:
                resolved_root = host_root.resolve(strict=False)
                resolved_path = (host_root / relative).resolve(strict=False)
            except (OSError, RuntimeError, ValueError):
                return None
            if not resolved_path.is_relative_to(resolved_root):
                return None
            if not self._include_missing_files and not resolved_path.is_file():
                return None
            mapped_path = host_root / relative if self._include_missing_files else resolved_path
            mapped_status = self._root_claim_status(host_root / relative, resolved_path)
            if mapped_status == "allowed":
                return _ResolvedMylarPath(
                    path=mapped_path,
                    status="mapped",
                    mapping_applied=True,
                )
            return (
                _ResolvedMylarPath(
                    path=mapped_path,
                    status="ambiguous" if mapped_status == "ambiguous" else "outside_root",
                    mapping_applied=True,
                    reason=(
                        "source_root_ambiguous"
                        if mapped_status == "ambiguous"
                        else "source_outside_root"
                    ),
                )
                if self._include_missing_files
                else None
            )
        if self._include_missing_files:
            return _ResolvedMylarPath(
                path=path,
                status="outside_root" if identity_exists else "missing",
                mapping_applied=False,
                reason="source_outside_root" if identity_exists else "path_missing",
            )
        return None

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
        if ".." not in location_path.parts and location_path.resolve(strict=False).is_dir():
            return None
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

    def _read_issue_records_for_comic_ids(
        self,
        conn: sqlite3.Connection,
        comic_ids: set[int],
    ) -> dict[int, list[_MylarIssueRecord]]:
        """Read only issue identities owned by one bounded Comic page."""
        if not comic_ids:
            return {}
        records: dict[int, list[_MylarIssueRecord]] = {}
        self._append_issue_table_records(conn, records, comic_ids=comic_ids)
        self._append_annual_table_records(conn, records, comic_ids=comic_ids)
        return records

    def _existing_comic_ids(
        self,
        conn: sqlite3.Connection,
        comic_ids: set[int],
    ) -> set[int]:
        if not comic_ids:
            return set()
        where_clause, params = self._comic_id_filter(comic_ids)
        query = f"SELECT ComicID FROM comics{where_clause}"
        return {
            parsed
            for row in conn.execute(query, params)
            if (parsed := self._parse_cv_id(row["ComicID"])) is not None
        }

    def _read_cross_release_records(
        self,
        conn: sqlite3.Connection,
        release_comic_ids: set[int],
    ) -> list[tuple[int, _MylarIssueRecord]]:
        """Read Annual records whose release identity is in the current Comic page."""
        if not release_comic_ids or not self._table_has_columns(
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
            return []
        annual_columns = self._table_columns(conn, "annuals")
        base_query = (
            "SELECT IssueID, ComicID, Issue_Number, IssueName, IssueDate, Location, "
            "ReleaseComicID, ReleaseComicName FROM annuals"
            if "ReleaseComicName" in annual_columns
            else "SELECT IssueID, ComicID, Issue_Number, IssueName, IssueDate, Location, "
            "ReleaseComicID, NULL AS ReleaseComicName FROM annuals"
        )
        where_clause, params = self._comic_id_filter(
            release_comic_ids,
            column_name="ReleaseComicID",
        )
        query = f"{base_query}{where_clause}"
        records: list[tuple[int, _MylarIssueRecord]] = []
        for row in conn.execute(query, params):
            parsed = self._annual_record_from_row(row)
            if parsed is None or parsed[0] == parsed[1].series_cv_id:
                continue
            records.append(parsed)
        return records

    def _read_comic_rows_by_cv_ids(
        self,
        conn: sqlite3.Connection,
        comic_ids: set[int],
    ) -> dict[int, sqlite3.Row]:
        if not comic_ids:
            return {}
        where_clause, params = self._comic_id_filter(comic_ids)
        rows: dict[int, sqlite3.Row] = {}
        query = (
            "SELECT ComicID, ComicName, ComicYear, ComicPublisher, ComicLocation, "
            f"Status, Total FROM comics{where_clause} ORDER BY rowid"
        )
        cursor = conn.execute(query, params)
        for row in cursor:
            comic_id = self._parse_cv_id(row["ComicID"])
            if comic_id is not None:
                rows.setdefault(comic_id, row)
        return rows

    def _attach_cross_release_records(
        self,
        series_page: list[DiscoveredSeries],
        *,
        cross_release_records: list[tuple[int, _MylarIssueRecord]],
        owner_rows: dict[int, sqlite3.Row],
    ) -> None:
        """Attach cross-page Annual files to their existing release series cohort."""
        target_by_cv_id = {
            series.mylar3_cv_id: series for series in series_page if series.mylar3_cv_id is not None
        }
        for owning_comic_id, record in cross_release_records:
            target = target_by_cv_id.get(record.series_cv_id)
            owner_row = owner_rows.get(owning_comic_id)
            if target is None or owner_row is None or not record.location:
                continue
            owner_location = owner_row["ComicLocation"]
            owner_resolution = self._resolve_location_details(owner_location)
            candidate_resolution = self._selected_recorded_issue_path_details(
                record.location,
                owner_resolution,
            )
            candidate = candidate_resolution.path if candidate_resolution is not None else None
            if candidate is None or candidate.suffix.lower() not in COMIC_EXTENSIONS:
                continue
            existing_paths = {item.file_path for item in target.files}
            if str(candidate) in existing_paths:
                continue
            built_files = self._build_files(
                [candidate],
                source_location=owner_location,
                series_cv_id=target.mylar3_cv_id,
                series_name=target.raw_series_name,
                series_year=target.raw_year,
                series_publisher=target.raw_publisher,
                issue_records=[record],
                issue_count_hint=self._parse_positive_int(owner_row["Total"]),
                series_status=owner_row["Status"],
            )
            target.files.extend(built_files)
            target.file_count = len(target.files)
            target.sample_paths = [item.file_path for item in target.files[:5]]
            target.has_files = bool(target.files)
            target.diagnostics["source_issue_type"] = IssueType.ANNUAL.value

    def _comic_id_filter(
        self,
        comic_ids: set[int],
        *,
        column_name: str = "ComicID",
    ) -> tuple[str, tuple[str, ...]]:
        if column_name not in {"ComicID", "ReleaseComicID"}:
            raise ValueError("Unsupported Mylar Comic ID filter column")
        identifiers = tuple(
            identifier
            for comic_id in sorted(comic_ids)
            for identifier in (str(comic_id), f"{self.MYLAR3_CV_PREFIX}{comic_id}")
        )
        placeholders = ", ".join("?" for _identifier in identifiers)
        return f" WHERE {column_name} IN ({placeholders})", identifiers

    def _append_issue_table_records(
        self,
        conn: sqlite3.Connection,
        records: dict[int, list[_MylarIssueRecord]],
        *,
        comic_ids: set[int] | None = None,
    ) -> None:
        if not self._table_has_columns(
            conn,
            "issues",
            {"IssueID", "ComicID", "Issue_Number", "IssueName", "IssueDate", "Location"},
        ):
            return
        where_clause, params = (
            self._comic_id_filter(comic_ids) if comic_ids is not None else ("", ())
        )
        query = (
            "SELECT IssueID, ComicID, Issue_Number, IssueName, IssueDate, Location FROM issues"
            f"{where_clause}"
        )
        for row in conn.execute(query, params):
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
        *,
        comic_ids: set[int] | None = None,
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
        base_query = (
            "SELECT IssueID, ComicID, Issue_Number, IssueName, IssueDate, Location, "
            "ReleaseComicID, ReleaseComicName FROM annuals"
            if "ReleaseComicName" in annual_columns
            else "SELECT IssueID, ComicID, Issue_Number, IssueName, IssueDate, Location, "
            "ReleaseComicID, NULL AS ReleaseComicName FROM annuals"
        )
        where_clause, params = (
            self._comic_id_filter(comic_ids) if comic_ids is not None else ("", ())
        )
        query = f"{base_query}{where_clause}"
        for row in conn.execute(query, params):
            parsed = self._annual_record_from_row(row)
            if parsed is None:
                continue
            owning_comic_id, record = parsed
            records.setdefault(owning_comic_id, []).append(record)

    def _annual_record_from_row(
        self,
        row: sqlite3.Row,
    ) -> tuple[int, _MylarIssueRecord] | None:
        owning_comic_id = self._parse_cv_id(row["ComicID"])
        issue_id = self._parse_positive_int(row["IssueID"])
        release_comic_id = self._parse_cv_id(row["ReleaseComicID"])
        if owning_comic_id is None or issue_id is None:
            return None
        return owning_comic_id, _MylarIssueRecord(
            issue_id=issue_id,
            series_cv_id=release_comic_id or owning_comic_id,
            issue_number=row["Issue_Number"],
            title=row["IssueName"],
            release_date=row["IssueDate"],
            location=row["Location"],
            issue_type=IssueType.ANNUAL,
            series_name=row["ReleaseComicName"],
        )

    def _read_arc_settings(self) -> Mylar3ArcSettingsSnapshot:
        """Read only the allowlisted arc settings from a bounded config file."""
        config_path = self._config_path or self._db_path.with_name("config.ini")
        content, present, warnings = self._read_bounded_config(config_path)
        if content is None:
            return Mylar3ArcSettingsSnapshot(
                present=present,
                parse_warnings=tuple(warnings),
                values=self._arc_setting_values(None, warnings),
            )

        try:
            config_text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            warnings.append("config_decode_failed")
            return Mylar3ArcSettingsSnapshot(
                present=True,
                parse_warnings=tuple(warnings),
                values=self._arc_setting_values(None, warnings),
            )

        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read_string(config_text)
        except configparser.Error:
            warnings.append("config_parse_failed")
            return Mylar3ArcSettingsSnapshot(
                present=True,
                parse_warnings=tuple(warnings),
                values=self._arc_setting_values(None, warnings),
            )

        values = self._arc_setting_values(parser, warnings)
        return Mylar3ArcSettingsSnapshot(
            present=True,
            parse_warnings=tuple(warnings),
            values=values,
        )

    def _read_bounded_config(
        self,
        config_path: Path,
    ) -> tuple[bytes | None, bool, list[str]]:
        """Open one regular config file without following a symlink."""
        try:
            initial_stat = config_path.lstat()
        except FileNotFoundError:
            return None, False, []
        except OSError:
            return None, True, ["config_stat_failed"]
        if stat.S_ISLNK(initial_stat.st_mode):
            return None, True, ["config_symlink_rejected"]
        if not stat.S_ISREG(initial_stat.st_mode):
            return None, True, ["config_not_regular_file"]
        if initial_stat.st_size > self.MAX_CONFIG_BYTES:
            return None, True, ["config_too_large"]

        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(config_path, flags)
        except OSError:
            return None, True, ["config_open_failed"]

        try:
            opened_stat = os.fstat(descriptor)
            if not stat.S_ISREG(opened_stat.st_mode):
                return None, True, ["config_not_regular_file"]
            if opened_stat.st_size > self.MAX_CONFIG_BYTES:
                return None, True, ["config_too_large"]
            if (
                initial_stat.st_ino
                and opened_stat.st_ino
                and (initial_stat.st_dev, initial_stat.st_ino)
                != (opened_stat.st_dev, opened_stat.st_ino)
            ):
                return None, True, ["config_source_changed"]

            chunks: list[bytes] = []
            remaining = self.MAX_CONFIG_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
        except OSError:
            return None, True, ["config_read_failed"]
        finally:
            os.close(descriptor)

        if len(content) > self.MAX_CONFIG_BYTES:
            return None, True, ["config_too_large"]
        return content, True, []

    def _arc_setting_values(
        self,
        parser: configparser.ConfigParser | None,
        warnings: list[str],
    ) -> tuple[Mylar3ArcSettingValue, ...]:
        """Normalize only known keys while retaining their raw values."""
        sections = (
            {section.casefold(): section for section in parser.sections()}
            if parser is not None
            else {}
        )
        values: list[Mylar3ArcSettingValue] = []
        for spec in self.ARC_SETTING_SPECS:
            section = sections.get(spec.section.casefold())
            raw_value = (
                parser.get(section, spec.key, raw=True)
                if parser is not None
                and section is not None
                and parser.has_option(section, spec.key)
                else None
            )
            value, used_default = self._normalize_arc_setting(spec, raw_value, warnings)
            values.append(
                Mylar3ArcSettingValue(
                    key=spec.key,
                    section=spec.section,
                    value=value,
                    raw_value=raw_value,
                    used_default=used_default,
                )
            )
        return tuple(values)

    def _normalize_arc_setting(
        self,
        spec: _MylarArcSettingSpec,
        raw_value: str | None,
        warnings: list[str],
    ) -> tuple[bool | str | None, bool]:
        if raw_value is None:
            return spec.default, True
        if spec.kind == "bool":
            normalized = raw_value.strip().casefold()
            if normalized in {"1", "yes", "true", "on"}:
                return True, False
            if normalized in {"0", "no", "false", "off"}:
                return False, False
            warnings.append(f"invalid_boolean:{spec.key}")
            return spec.default, True
        if spec.key == "STORYARC_LOCATION" and raw_value.strip().casefold() in {"", "none"}:
            return None, False
        if spec.key == "ARC_FILEOPS" and raw_value.strip().casefold() not in {
            "copy",
            "move",
            "hardlink",
            "softlink",
        }:
            warnings.append("unknown_value:ARC_FILEOPS")
        return raw_value, False

    def _read_story_arc_rows(
        self,
        conn: sqlite3.Connection,
    ) -> tuple[list[sqlite3.Row], bool]:
        """Read available story-arc evidence without requiring a schema upgrade."""
        if not self._table_exists(conn, "storyarcs"):
            return [], False

        available_columns = {
            column.casefold(): column for column in self._table_columns(conn, "storyarcs")
        }
        expressions: list[str] = []
        for expected_column in self.STORY_ARC_COLUMNS:
            actual_column = available_columns.get(expected_column.casefold())
            if actual_column is None:
                expressions.append(f'NULL AS "{expected_column}"')
                continue
            quoted_column = actual_column.replace('"', '""')
            expressions.append(f'"{quoted_column}" AS "{expected_column}"')

        select_columns = ", ".join(expressions)
        query_with_rowid = f'SELECT {select_columns}, rowid AS "__source_rowid" FROM storyarcs'
        try:
            rows = conn.execute(query_with_rowid).fetchall()
        except sqlite3.OperationalError:
            query_without_rowid = (
                f'SELECT {select_columns}, NULL AS "__source_rowid" FROM storyarcs'
            )
            rows = conn.execute(query_without_rowid).fetchall()
        return rows, True

    def _read_readlist_count(self, conn: sqlite3.Connection) -> tuple[bool, int]:
        """Inventory Mylar's distinct personal read list without importing it."""
        if not self._table_exists(conn, "readlist"):
            return False, 0
        row = conn.execute("SELECT COUNT(*) AS row_count FROM readlist").fetchone()
        return True, int(row["row_count"]) if row is not None else 0

    def _convert_story_arc_rows(
        self,
        rows: list[sqlite3.Row],
    ) -> tuple[Mylar3StoryArcSnapshot, ...]:
        """Group raw Mylar rows while retaining unresolved and duplicate entries."""
        grouped: dict[tuple[str, str], list[tuple[int, sqlite3.Row]]] = {}
        for source_index, row in enumerate(rows):
            group_key = self._story_arc_group_key(row, source_index)
            grouped.setdefault(group_key, []).append((source_index, row))

        arcs: list[Mylar3StoryArcSnapshot] = []
        for grouped_rows in grouped.values():
            ordered_rows = sorted(grouped_rows, key=self._story_arc_row_sort_key)
            entries = tuple(
                self._story_arc_entry(row, ordinal=ordinal)
                for ordinal, (_source_index, row) in enumerate(ordered_rows, start=1)
            )
            arcs.append(
                Mylar3StoryArcSnapshot(
                    story_arc_id=self._first_story_arc_value(entries, "story_arc_id"),
                    cv_arc_id=self._first_story_arc_value(entries, "cv_arc_id"),
                    name=self._first_story_arc_value(entries, "story_arc_name"),
                    entries=entries,
                )
            )

        return tuple(
            sorted(
                arcs,
                key=lambda arc: (
                    arc.name is None,
                    (arc.name or "").casefold(),
                    arc.story_arc_id or "",
                    arc.cv_arc_id or "",
                ),
            )
        )

    def _story_arc_group_key(
        self,
        row: sqlite3.Row,
        source_index: int,
    ) -> tuple[str, str]:
        story_arc_id = self._optional_text(row["StoryArcID"])
        if story_arc_id:
            return "story_arc_id", story_arc_id
        cv_arc_id = self._optional_text(row["CV_ArcID"])
        if cv_arc_id:
            return "cv_arc_id", cv_arc_id
        story_arc_name = self._optional_text(row["StoryArc"])
        if story_arc_name:
            return "story_arc_name", story_arc_name
        source_rowid = self._parse_optional_int(row["__source_rowid"])
        return "unidentified_row", str(source_rowid if source_rowid is not None else source_index)

    def _story_arc_row_sort_key(
        self,
        item: tuple[int, sqlite3.Row],
    ) -> tuple[object, ...]:
        source_index, row = item
        reading_order = self._parse_optional_int(row["ReadingOrder"])
        source_rowid = self._parse_optional_int(row["__source_rowid"])
        return (
            reading_order is None,
            reading_order if reading_order is not None else 0,
            source_rowid is None,
            source_rowid if source_rowid is not None else 0,
            self._optional_text(row["IssueArcID"]) or "",
            self._optional_text(row["IssueID"]) or "",
            self._optional_text(row["ComicID"]) or "",
            self._optional_text(row["IssueNumber"]) or "",
            source_index,
        )

    def _story_arc_entry(
        self,
        row: sqlite3.Row,
        *,
        ordinal: int,
    ) -> Mylar3StoryArcEntrySnapshot:
        reading_order_raw = self._optional_text(row["ReadingOrder"])
        return Mylar3StoryArcEntrySnapshot(
            ordinal=ordinal,
            reading_order=self._parse_optional_int(reading_order_raw),
            reading_order_raw=reading_order_raw,
            story_arc_id=self._optional_text(row["StoryArcID"]),
            story_arc_name=self._optional_text(row["StoryArc"]),
            cv_arc_id=self._optional_text(row["CV_ArcID"]),
            issue_arc_id=self._optional_text(row["IssueArcID"]),
            issue_id=self._optional_text(row["IssueID"]),
            comic_id=self._optional_text(row["ComicID"]),
            issue_number=self._optional_text(row["IssueNumber"]),
            comic_name=self._optional_text(row["ComicName"]),
            series_year=self._optional_text(row["SeriesYear"]),
            issue_year=self._optional_text(row["IssueYEAR"]),
            status=self._optional_text(row["Status"]),
            location=self._optional_text(row["Location"]),
            release_date=self._optional_text(row["ReleaseDate"]),
            issue_date=self._optional_text(row["IssueDate"]),
            publisher=self._optional_text(row["Publisher"]),
            issue_publisher=self._optional_text(row["IssuePublisher"]),
            issue_name=self._optional_text(row["IssueName"]),
            manual=self._optional_text(row["Manual"]),
            date_added=self._optional_text(row["DateAdded"]),
            digital_date=self._optional_text(row["DigitalDate"]),
            issue_type=self._optional_text(row["Type"]),
            aliases=self._optional_text(row["Aliases"]),
            total_issues=self._optional_text(row["TotalIssues"]),
            in_cache_dir=self._optional_text(row["inCacheDir"]),
            int_issue_number=self._optional_text(row["Int_IssueNumber"]),
            dynamic_comic_name=self._optional_text(row["DynamicComicName"]),
            volume=self._optional_text(row["Volume"]),
            arc_image=self._optional_text(row["ArcImage"]),
        )

    def _first_story_arc_value(
        self,
        entries: tuple[Mylar3StoryArcEntrySnapshot, ...],
        attribute_name: str,
    ) -> str | None:
        for entry in entries:
            value = getattr(entry, attribute_name)
            if isinstance(value, str) and value:
                return value
        return None

    def _optional_text(self, value: object) -> str | None:
        return None if value is None else str(value)

    def _parse_optional_int(self, value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    def _table_exists(self, conn: sqlite3.Connection, table_name: str) -> bool:
        if table_name not in {"storyarcs", "readlist"}:
            return False
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? COLLATE NOCASE",
            (table_name,),
        ).fetchone()
        return row is not None

    def _table_has_columns(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        required_columns: set[str],
    ) -> bool:
        available = {column.casefold() for column in self._table_columns(conn, table_name)}
        return {column.casefold() for column in required_columns}.issubset(available)

    def _table_columns(self, conn: sqlite3.Connection, table_name: str) -> set[str]:
        queries = {
            "issues": "PRAGMA table_info(issues)",
            "annuals": "PRAGMA table_info(annuals)",
            "storyarcs": "PRAGMA table_info(storyarcs)",
            "readlist": "PRAGMA table_info(readlist)",
        }
        query = queries.get(table_name)
        if query is None:
            return set()
        rows = list(conn.execute(query))
        return {str(row["name"]) for row in rows}

    def _get_resolved_file_count(self, resolved_path: Path | None) -> int:
        """Count comic files in one validated resolved directory."""
        if resolved_path is None:
            return 0
        return sum(1 for f in resolved_path.iterdir() if f.suffix.lower() in COMIC_EXTENSIONS)
