"""Benchmark a large trusted Mylar migration through Pullbox Step 2.

The default fixture mirrors the public issue #111 shape: 463 source series,
2,315 files, and 47 Annual rows whose ComicVine release series differ from
their owning Mylar series.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import structlog
from mylar3_import_fixture import create_scaled_mylar3_fixture
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullbox.core.events import EventBus
from pullbox.models import import_job as _import_job_models  # noqa: F401
from pullbox.models import issue as _issue_models  # noqa: F401
from pullbox.models import library as _library_models  # noqa: F401
from pullbox.models import publisher as _publisher_models  # noqa: F401
from pullbox.models import series as _series_models  # noqa: F401
from pullbox.models.base import Base
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.performance.baseline import current_process_peak_rss_bytes
from pullbox.services.import_service import ImportService


class NoExternalMetadataProvider:
    """Fail and record if known Mylar state attempts external metadata I/O."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def _unexpected(self, method_name: str) -> Any:
        self.calls.append(method_name)
        raise AssertionError(f"Unexpected metadata provider call: {method_name}")

    async def search_series(self, *_args: Any, **_kwargs: Any) -> Any:
        return await self._unexpected("search_series")

    async def get_series(self, *_args: Any, **_kwargs: Any) -> Any:
        return await self._unexpected("get_series")

    async def get_issue(self, *_args: Any, **_kwargs: Any) -> Any:
        return await self._unexpected("get_issue")

    async def get_issues_for_series(self, *_args: Any, **_kwargs: Any) -> Any:
        return await self._unexpected("get_issues_for_series")

    async def get_issues_for_series_by_numbers(self, *_args: Any, **_kwargs: Any) -> Any:
        return await self._unexpected("get_issues_for_series_by_numbers")


async def _count_rows(session: Any, model: type[Any], *criteria: Any) -> int:
    result = await session.scalar(select(func.count()).select_from(model).where(*criteria))
    return int(result or 0)


async def _run_benchmark(args: argparse.Namespace, workspace: Path) -> dict[str, Any]:
    fixture = create_scaled_mylar3_fixture(
        workspace,
        series_count=args.series_count,
        files_per_series=args.files_per_series,
        annual_count=args.annual_count,
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    provider = NoExternalMetadataProvider()
    service = ImportService(
        series_service=cast("Any", SimpleNamespace()),
        metadata_service=cast("Any", SimpleNamespace(_provider=provider)),
        event_bus=EventBus(),
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    error: str | None = None
    try:
        async with session_factory() as session:
            job = ImportJob(
                source_path=str(fixture.db_path),
                source_type=ImportSourceType.MYLAR3,
                status=ImportJobStatus.PENDING,
            )
            session.add(job)
            await session.commit()

            started_at = time.monotonic()
            try:
                await service.start_scan(session, job.id)
            except Exception as exc:  # benchmark reports failures before exiting
                error = f"{type(exc).__name__}: {exc}"
            elapsed_ms = round((time.monotonic() - started_at) * 1000)
            await session.refresh(job)

            report = {
                "elapsed_ms": elapsed_ms,
                "source_series_count": fixture.source_series_count,
                "expected_discovered_series_count": fixture.discovered_series_count,
                "expected_file_count": fixture.file_count,
                "annual_count": fixture.annual_count,
                "materialized_series_count": await _count_rows(
                    session,
                    ImportedSeries,
                    ImportedSeries.import_job_id == job.id,
                ),
                "materialized_file_count": await _count_rows(
                    session,
                    ImportedFile,
                    ImportedFile.import_job_id == job.id,
                ),
                "matched_file_count": await _count_rows(
                    session,
                    ImportedFile,
                    ImportedFile.import_job_id == job.id,
                    ImportedFile.status == ImportedFileStatus.MATCHED,
                ),
                "provider_calls": provider.calls,
                "provider_call_count": len(provider.calls),
                "peak_rss_bytes": current_process_peak_rss_bytes(),
                "final_status": job.status.value,
                "error": error,
            }
    finally:
        await engine.dispose()
    return report


def _validate_report(report: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if report["error"] is not None:
        failures.append(str(report["error"]))
    if report["final_status"] != ImportJobStatus.REVIEW.value:
        failures.append(f"final status was {report['final_status']}, expected review")
    if report["provider_call_count"] != 0:
        failures.append(f"metadata provider calls occurred: {report['provider_calls']}")
    if report["materialized_series_count"] != report["expected_discovered_series_count"]:
        failures.append("materialized series count did not match the generated fixture")
    if report["materialized_file_count"] != report["expected_file_count"]:
        failures.append("materialized file count did not match the generated fixture")
    if report["matched_file_count"] != report["expected_file_count"]:
        failures.append("not every trusted Mylar file reached review as matched")
    return failures


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--series-count", type=int, default=463)
    parser.add_argument("--files-per-series", type=int, default=5)
    parser.add_argument("--annual-count", type=int, default=47)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if not args.verbose:
        structlog.configure(
            wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
        )

    with tempfile.TemporaryDirectory(prefix="pullbox-mylar-bench-") as temporary_dir:
        report = await _run_benchmark(args, Path(temporary_dir))

    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{rendered}\n", encoding="utf-8")
    failures = _validate_report(report)
    if failures:
        raise SystemExit("Mylar benchmark failed: " + "; ".join(failures))


if __name__ == "__main__":
    asyncio.run(main())
