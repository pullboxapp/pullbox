"""Bounded SQLite reads from an immutable catalog generation."""

from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import structlog

from pullbox.config import get_settings
from pullbox.core.issue_numbers import parse_issue_number_text
from pullbox.core.naming import detect_issue_type_from_metadata_title
from pullbox.providers.base import IssueMetadata, IssueSummary, SeriesMetadata, SeriesSearchResult
from pullbox.services.catalog.contract import CatalogError, valid_version
from pullbox.services.catalog.database import open_readonly, safe_path, validate_snapshot
from pullbox.services.catalog.storage import disk_work, load_json

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger(__name__)
SERIES_COLUMNS = "s.id,s.name,s.start_year,p.name,s.issue_count,s.cover_url"
SERIES_FROM = "series s LEFT JOIN publishers p ON p.id=s.publisher_id"
ISSUE_COLUMNS = (
    "id,series_id,issue_number,normalized_issue_number,sort_number,"
    "title,cover_date,store_date,cover_url"
)


@dataclass(frozen=True)
class CatalogSeriesMetadata(SeriesMetadata):
    source_cutoff_at: datetime | None = None


@dataclass(frozen=True)
class CatalogIssueSummary(IssueSummary):
    source_cutoff_at: datetime | None = None


class CatalogReader:
    """Pin a file per query; verify a generation once before serving its rows."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._validated: set[tuple[str, int, int, int]] = set()
        self._validation_lock = threading.Lock()

    @property
    def available(self) -> bool:
        return (self.root / "active.json").exists()

    def _generation(self) -> tuple[Path, datetime]:
        reference = load_json(self.root / "active.json")
        version = valid_version(reference.get("version"))
        relative = reference.get("path")
        if relative not in {f"bases/{version}.db", f"versions/{version}.db"}:
            raise CatalogError("Catalog active file is invalid. Retry the catalog update.")
        path = safe_path(self.root / str(relative))
        stat = path.stat()
        identity = (str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size)
        with self._validation_lock:
            if identity not in self._validated:
                validate_snapshot(path, version)
                if len(self._validated) >= 8:
                    self._validated.clear()
                self._validated.add(identity)
        return path, datetime.fromisoformat(str(reference["source_cutoff_at"]))

    def _query(self, sql: str, params: tuple[object, ...]) -> tuple[list[Any], datetime]:
        try:
            path, cutoff = self._generation()
            with closing(open_readonly(path)) as db:
                return db.execute(sql, params).fetchall(), cutoff
        except (sqlite3.Error, OSError, KeyError) as exc:
            raise CatalogError(
                "The local catalog could not be read. Retry its update in Metadata settings."
            ) from exc

    async def search(
        self, query: str, year: int | None = None, limit: int = 1000, offset: int = 0
    ) -> list[SeriesSearchResult]:
        terms = re.findall(r"\w+", query[:256], flags=re.UNICODE)[:16]
        if not terms:
            return []
        expression = " AND ".join(f'"{term}"*' for term in terms)
        rows, _ = await disk_work(
            self._query,
            f"SELECT {SERIES_COLUMNS} FROM series_fts f JOIN series s ON s.id=f.rowid "
            "LEFT JOIN publishers p ON p.id=s.publisher_id "
            "WHERE series_fts MATCH ? AND (? IS NULL OR s.start_year=?) "
            "ORDER BY rank,s.id LIMIT ? OFFSET ?",
            (expression, year, year, max(1, min(limit, 1000)), max(0, min(offset, 10000))),
        )
        return [
            SeriesSearchResult(
                str(r[0]),
                r[1],
                r[2],
                r[3],
                r[4],
                None,
                r[5],
                None,
                f"https://comicvine.gamespot.com/volume/4050-{r[0]}/",
            )
            for r in rows
        ]

    async def series(self, series_id: int) -> CatalogSeriesMetadata | None:
        rows, cutoff = await disk_work(
            self._query, f"SELECT {SERIES_COLUMNS} FROM {SERIES_FROM} WHERE s.id=?", (series_id,)
        )
        if not rows:
            return None
        r = rows[0]
        return CatalogSeriesMetadata(
            str(r[0]),
            r[1],
            r[1],
            r[2],
            None,
            None,
            r[3],
            None,
            r[5],
            r[4],
            f"https://comicvine.gamespot.com/volume/4050-{r[0]}/",
            cutoff,
        )

    async def issues(self, series_id: int) -> list[IssueSummary]:
        rows, cutoff = await disk_work(
            self._query,
            f"SELECT {ISSUE_COLUMNS} FROM issues WHERE series_id=? "
            "ORDER BY CAST(sort_number AS REAL),issue_number,id",
            (series_id,),
        )
        return [self._summary(row, cutoff) for row in rows]

    @staticmethod
    def _summary(row: Any, cutoff: datetime) -> CatalogIssueSummary:
        try:
            number, exact = parse_issue_number_text(str(row[2] or row[3] or "0"))
        except ValueError:
            number, exact = 0.0, None
        return CatalogIssueSummary(
            str(row[0]),
            number,
            row[5],
            row[6],
            row[8],
            detect_issue_type_from_metadata_title(row[5] or ""),
            exact,
            cutoff,
        )

    async def issue(self, issue_id: int) -> IssueMetadata | None:
        """Return only the basic identity fields used during import file matching."""
        rows, cutoff = await disk_work(
            self._query, f"SELECT {ISSUE_COLUMNS} FROM issues WHERE id=?", (issue_id,)
        )
        if not rows:
            return None
        row = rows[0]
        summary = self._summary(row, cutoff)
        return IssueMetadata(
            summary.provider_id,
            str(row[1]),
            summary.issue_number,
            summary.title,
            None,
            summary.release_date,
            row[7],
            summary.cover_url,
            None,
            f"https://comicvine.gamespot.com/issue/4000-{row[0]}/",
            issue_number_text=summary.issue_number_text,
        )


@lru_cache(maxsize=4)
def _reader(root: Path) -> CatalogReader:
    return CatalogReader(root)


def get_catalog_reader() -> CatalogReader:
    return _reader(get_settings().data_dir / "catalog")
