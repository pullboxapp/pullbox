"""Bounded SQLite reads from an immutable catalog generation."""

from __future__ import annotations

import hashlib
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
    from collections.abc import Sequence
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


@dataclass(frozen=True)
class CatalogIssueMetadata(IssueMetadata):
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

    async def cache_token(self) -> str | None:
        """Fingerprint the validated active file without exposing its location."""

        def token() -> str | None:
            if not self.available:
                return None
            path, cutoff = self._generation()
            stat = path.stat()
            identity = (str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size, cutoff.isoformat())
            return hashlib.sha256(repr(identity).encode()).hexdigest()

        return await disk_work(token)

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

    async def issue(
        self, issue_id: int, *, preserve_number_text: bool = False
    ) -> IssueMetadata | None:
        """Return only the basic identity fields used during import file matching."""
        rows, cutoff = await disk_work(
            self._query, f"SELECT {ISSUE_COLUMNS} FROM issues WHERE id=?", (issue_id,)
        )
        if not rows:
            return None
        row = rows[0]
        return self._issue_metadata(row, cutoff, preserve_number_text=preserve_number_text)

    async def issue_batch(self, issue_ids: Sequence[int]) -> dict[str, IssueMetadata]:
        """Resolve exact issue identities in bounded, read-only catalog queries."""
        ids = list(dict.fromkeys(issue_ids))
        found: dict[str, IssueMetadata] = {}
        for offset in range(0, len(ids), 200):
            batch = ids[offset : offset + 200]
            placeholders = ",".join("?" for _ in batch)
            rows, cutoff = await disk_work(
                self._query,
                f"SELECT {ISSUE_COLUMNS} FROM issues WHERE id IN ({placeholders})",
                tuple(batch),
            )
            for row in rows:
                issue = self._issue_metadata(row, cutoff, preserve_number_text=False)
                found[issue.provider_id] = issue
        return found

    def _issue_metadata(
        self, row: Any, cutoff: datetime, *, preserve_number_text: bool
    ) -> CatalogIssueMetadata:
        summary = self._summary(row, cutoff)
        return CatalogIssueMetadata(
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
            issue_number_text=(
                str(row[2] or row[3] or "") if preserve_number_text else summary.issue_number_text
            ),
            source_cutoff_at=cutoff,
        )

    async def recent_issues(self, series_id: int) -> tuple[list[IssueMetadata], int, datetime]:
        """Newest publication slice and total, pinned to one immutable generation."""
        if type(series_id) is not int or not 0 < series_id < 2**63:
            raise ValueError("Invalid catalog series identity")
        rows, cutoff = await disk_work(
            self._query,
            f"WITH page AS (SELECT {ISSUE_COLUMNS} FROM issues WHERE series_id=? "
            "ORDER BY store_date DESC,id DESC LIMIT 100), "
            "tally AS (SELECT COUNT(*) AS total FROM issues WHERE series_id=?) "
            "SELECT page.*,tally.total FROM tally LEFT JOIN page ON 1=1 "
            "ORDER BY page.store_date DESC,page.id DESC",
            (series_id, series_id),
        )
        return (
            [
                self._issue_metadata(row, cutoff, preserve_number_text=True)
                for row in rows
                if row[0] is not None
            ],
            int(rows[0][-1]),
            cutoff,
        )

    async def issue_page(self, series_id: int, *, page: int = 1) -> tuple[list[IssueMetadata], int]:
        """Count and read a bounded page in one query on one immutable generation."""
        if type(page) is not int or not 1 <= page <= 10000:
            raise ValueError("Invalid catalog page")
        rows, cutoff = await disk_work(
            self._query,
            f"WITH page AS (SELECT {ISSUE_COLUMNS} FROM issues WHERE series_id=? "
            "ORDER BY id LIMIT 100 OFFSET ?), "
            "tally AS (SELECT COUNT(*) AS total FROM issues WHERE series_id=?) "
            "SELECT page.*,tally.total FROM tally LEFT JOIN page ON 1=1 ORDER BY page.id",
            (series_id, (page - 1) * 100, series_id),
        )
        return (
            [
                self._issue_metadata(row, cutoff, preserve_number_text=True)
                for row in rows
                if row[0] is not None
            ],
            int(rows[0][-1]),
        )


@lru_cache(maxsize=4)
def _reader(root: Path) -> CatalogReader:
    return CatalogReader(root)


def get_catalog_reader() -> CatalogReader:
    return _reader(get_settings().data_dir / "catalog")
