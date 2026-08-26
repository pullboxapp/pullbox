"""Benchmark the Step 2 import scan/review pipeline on a synthetic tree.

Usage:
  .venv/bin/python scripts/benchmark_import_scan.py --series-count 200 --files-per-series 12
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullbox.core.source_metadata import SourceMetadataExtractor
from pullbox.models import import_job as _import_job_models  # noqa: F401
from pullbox.models import issue as _issue_models  # noqa: F401
from pullbox.models import library as _library_models  # noqa: F401
from pullbox.models import publisher as _publisher_models  # noqa: F401
from pullbox.models import series as _series_models  # noqa: F401
from pullbox.models.base import Base
from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType
from pullbox.performance.baseline import current_process_peak_rss_bytes
from pullbox.providers.base import IssueSummary, SeriesSearchResult
from pullbox.services.import_provider_cache import CachedImportMetadataProvider
from pullbox.services.import_service import ImportService


class FakeMetadataProvider:
    """Deterministic provider that counts calls during benchmark runs."""

    def __init__(self) -> None:
        self.search_calls = 0
        self.series_calls = 0
        self.issue_summary_calls = 0
        self.issue_number_calls = 0

    @staticmethod
    def _series_provider_id(query: str, year: int | None) -> str:
        try:
            series_number = int(query.rsplit(" ", 1)[-1])
        except ValueError:
            series_number = sum(ord(char) for char in query) + (year or 0)
        return str(100000 + series_number)

    async def search_series(
        self,
        query: str,
        year: int | None = None,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> list[SeriesSearchResult]:
        self.search_calls += 1
        provider_id = self._series_provider_id(query, year)
        return [
            SeriesSearchResult(
                provider_id=provider_id,
                title=query,
                year_start=year,
                publisher="Benchmark Comics",
                issue_count=200,
                status="ended",
                cover_url=None,
                description=None,
                comicvine_url=f"https://example.com/{provider_id}",
            )
        ]

    async def get_series(self, provider_id: str) -> SimpleNamespace:
        self.series_calls += 1
        return SimpleNamespace(
            provider_id=provider_id,
            title=f"Series {provider_id}",
            year_start=2020,
            publisher="Benchmark Comics",
            issue_count=200,
            comicvine_url=f"https://example.com/{provider_id}",
        )

    def _issue_summary(self, series_provider_id: str, issue_number: float) -> IssueSummary:
        series_id = int(series_provider_id)
        issue_idx = int(issue_number)
        return IssueSummary(
            provider_id=str((series_id * 1000) + issue_idx),
            issue_number=float(issue_idx),
            title=f"Issue {issue_idx}",
            release_date=None,
            cover_url=None,
            issue_type="issue",
        )

    async def get_issues_for_series(self, series_provider_id: str) -> list[IssueSummary]:
        self.issue_summary_calls += 1
        return [self._issue_summary(series_provider_id, float(idx)) for idx in range(1, 21)]

    async def get_issues_for_series_by_numbers(
        self,
        series_provider_id: str,
        issue_numbers: list[float],
    ) -> list[IssueSummary]:
        self.issue_number_calls += 1
        return [
            self._issue_summary(series_provider_id, issue_number)
            for issue_number in sorted({float(number) for number in issue_numbers})
        ]


def _build_tree(
    root: Path,
    *,
    series_count: int,
    files_per_series: int,
    trusted_comicinfo: bool,
) -> None:
    for series_idx in range(series_count):
        title = f"Series {series_idx:04d}"
        year = 2000 + (series_idx % 20)
        series_provider_id = 100000 + series_idx
        folder = root / f"{title} ({year})"
        folder.mkdir(parents=True, exist_ok=True)
        for file_idx in range(1, files_per_series + 1):
            archive_path = folder / f"{title} #{file_idx:03d}.cbz"
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    f"{title} #{file_idx:03d}.jpg",
                    b"benchmark-page",
                )
                if trusted_comicinfo:
                    issue_provider_id = (series_provider_id * 1000) + file_idx
                    archive.writestr(
                        "ComicInfo.xml",
                        (
                            "<?xml version='1.0'?>"
                            "<ComicInfo>"
                            f"<Series>{title}</Series>"
                            f"<Number>{file_idx}</Number>"
                            f"<Volume>{year}</Volume>"
                            "<Notes>"
                            f"[cv_vol_id:{series_provider_id}] "
                            f"[cv_issue_id:{issue_provider_id}]"
                            "</Notes>"
                            "</ComicInfo>"
                        ),
                    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--series-count", type=int, default=200)
    parser.add_argument("--files-per-series", type=int, default=12)
    parser.add_argument("--trusted-comicinfo", action="store_true")
    args = parser.parse_args()

    provider = FakeMetadataProvider()
    metadata_service = SimpleNamespace(_provider=provider)
    service = ImportService(
        series_service=cast("Any", SimpleNamespace()),
        metadata_service=cast("Any", metadata_service),
        event_bus=cast("Any", SimpleNamespace()),
    )
    benchmark_service = cast("Any", service)
    benchmark_service._build_scan_metadata_provider = lambda _session: CachedImportMetadataProvider(
        provider
    )

    archive_read_count = 0
    archive_entry_issue_hint_count = 0
    commit_count = 0
    original_read_archive_comicinfo = SourceMetadataExtractor._read_archive_comicinfo
    original_archive_entry_issue_hint_from_path = (
        SourceMetadataExtractor.archive_entry_issue_hint_from_path
    )

    def counting_read_archive_comicinfo(path: Path) -> Any:
        nonlocal archive_read_count
        archive_read_count += 1
        return original_read_archive_comicinfo(path)

    def counting_archive_entry_issue_hint_from_path(
        path: str | Path,
        *,
        expected_series_name: str | None = None,
    ) -> Any:
        nonlocal archive_entry_issue_hint_count
        archive_entry_issue_hint_count += 1
        return original_archive_entry_issue_hint_from_path(
            path,
            expected_series_name=expected_series_name,
        )

    SourceMetadataExtractor._read_archive_comicinfo = staticmethod(  # type: ignore[method-assign]
        counting_read_archive_comicinfo
    )
    SourceMetadataExtractor.archive_entry_issue_hint_from_path = staticmethod(  # type: ignore[method-assign]
        counting_archive_entry_issue_hint_from_path
    )
    try:
        with tempfile.TemporaryDirectory(prefix="pullbox-scan-bench-") as tmp:
            root = Path(tmp) / "imports"
            root.mkdir(parents=True, exist_ok=True)
            _build_tree(
                root,
                series_count=args.series_count,
                files_per_series=args.files_per_series,
                trusted_comicinfo=args.trusted_comicinfo,
            )

            db_path = Path(tmp) / "benchmark.db"
            engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as session:
                original_commit = session.commit

                async def counted_commit() -> None:
                    nonlocal commit_count
                    commit_count += 1
                    await original_commit()

                session.commit = counted_commit  # type: ignore[method-assign]

                job = ImportJob(
                    source_path=str(root),
                    source_type=ImportSourceType.FILESYSTEM,
                    status=ImportJobStatus.PENDING,
                    min_files_per_series=1,
                )
                session.add(job)
                await session.commit()

                started_at = time.monotonic()
                await service.start_scan(session, job.id)
                elapsed_ms = round((time.monotonic() - started_at) * 1000)

                report = {
                    "series_count": args.series_count,
                    "files_per_series": args.files_per_series,
                    "trusted_comicinfo": args.trusted_comicinfo,
                    "elapsed_ms": elapsed_ms,
                    "archive_read_count": archive_read_count,
                    "archive_entry_issue_hint_count": archive_entry_issue_hint_count,
                    "provider_search_calls": provider.search_calls,
                    "provider_get_series_calls": provider.series_calls,
                    "provider_issue_summary_calls": provider.issue_summary_calls,
                    "provider_issue_number_calls": provider.issue_number_calls,
                    "db_commit_count": commit_count,
                    "peak_rss_bytes": current_process_peak_rss_bytes(),
                    "final_status": job.status.value,
                    "scan_total_dirs": job.scan_total_dirs,
                    "scan_total_files": job.scan_total_files,
                    "series_found": job.series_found,
                    "series_matched": job.series_matched,
                    "series_no_match": job.series_no_match,
                    "total_files_matched": job.total_files_matched,
                    "total_files_no_match": job.total_files_no_match,
                    "total_files_conflict": job.total_files_conflict,
                }
                print(json.dumps(report, indent=2, sort_keys=True))

            await engine.dispose()
    finally:
        SourceMetadataExtractor._read_archive_comicinfo = staticmethod(  # type: ignore[method-assign]
            original_read_archive_comicinfo
        )
        SourceMetadataExtractor.archive_entry_issue_hint_from_path = staticmethod(  # type: ignore[method-assign]
            original_archive_entry_issue_hint_from_path
        )


if __name__ == "__main__":
    asyncio.run(main())
