"""Benchmark the Step 4 import execution pipeline on synthetic review data.

Usage:
  .venv/bin/python scripts/benchmark_import_execute.py --series-count 40 --files-per-series 4
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import tempfile
import time
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.core.library_policy import LibraryIngestPolicy, serialize_library_ingest_policy
from pullbox.models import config as _config_models  # noqa: F401
from pullbox.models import import_job as _import_job_models  # noqa: F401
from pullbox.models import issue as _issue_models  # noqa: F401
from pullbox.models import library as _library_models  # noqa: F401
from pullbox.models import publisher as _publisher_models  # noqa: F401
from pullbox.models import series as _series_models  # noqa: F401
from pullbox.models.base import Base
from pullbox.models.config import SystemConfig
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.operation_progress import OperationProgress
from pullbox.models.series import Series
from pullbox.performance.baseline import current_process_peak_rss_bytes
from pullbox.services.import_service import ImportService

_REPORT_DIGEST_PAGE_SIZE = 1_000
_MAX_REPORT_SAMPLE_LIMIT = 100


def _bounded_sample_limit(value: str) -> int:
    limit = int(value)
    if not 0 <= limit <= _MAX_REPORT_SAMPLE_LIMIT:
        raise argparse.ArgumentTypeError(
            f"report sample limit must be between 0 and {_MAX_REPORT_SAMPLE_LIMIT}"
        )
    return limit


async def _summarize_library_files(
    session: AsyncSession,
    *,
    sample_limit: int,
) -> dict[str, object]:
    """Return bounded examples and a digest without materializing every row."""
    library_file_count = int(await session.scalar(select(func.count(LibraryFile.id))) or 0)
    format_rows = (
        await session.execute(
            select(LibraryFile.file_format, func.count(LibraryFile.id))
            .group_by(LibraryFile.file_format)
            .order_by(LibraryFile.file_format)
        )
    ).all()
    library_format_counts = {
        file_format.value.lower(): int(count) for file_format, count in format_rows
    }
    name_samples = list(
        (
            await session.scalars(
                select(LibraryFile.file_name)
                .order_by(LibraryFile.file_name, LibraryFile.id)
                .limit(sample_limit)
            )
        ).all()
    )

    digest = hashlib.sha256()
    last_id = 0
    while True:
        page = (
            await session.execute(
                select(LibraryFile.id, LibraryFile.file_name)
                .where(LibraryFile.id > last_id)
                .order_by(LibraryFile.id)
                .limit(_REPORT_DIGEST_PAGE_SIZE)
            )
        ).all()
        if not page:
            break
        for library_file_id, file_name in page:
            encoded = file_name.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, byteorder="big"))
            digest.update(encoded)
            last_id = library_file_id

    return {
        "library_file_count": library_file_count,
        "library_file_name_sample_limit": sample_limit,
        "library_file_name_samples": name_samples,
        "library_file_name_digest_sha256": digest.hexdigest(),
        "library_format_counts": dict(sorted(library_format_counts.items())),
    }


class FakeSeriesService:
    """Deterministic Step 4 series service used for import benchmarking."""

    def __init__(self, files_per_series: int) -> None:
        self.files_per_series = files_per_series
        self.prefetch_calls = 0
        self.add_calls = 0

    async def prefetch_comicvine_bundle(
        self,
        comicvine_id: int,
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        self.prefetch_calls += 1
        return (
            {
                "comicvine_id": comicvine_id,
                "title": f"Series {comicvine_id}",
                "year_start": 2020,
            },
            [
                {
                    "comicvine_id": comicvine_id * 1000 + issue_idx,
                    "issue_number": float(issue_idx),
                    "title": f"Issue {issue_idx}",
                }
                for issue_idx in range(1, self.files_per_series + 1)
            ],
        )

    async def add_from_comicvine_prefetched(
        self,
        session: AsyncSession,
        *,
        comicvine_id: int,
        library_root_id: int | None,
        search_on_add: bool,
        series_meta: dict[str, Any],
        issue_summaries: list[dict[str, Any]],
    ) -> Series:
        _ = library_root_id, search_on_add
        self.add_calls += 1
        existing = await session.scalar(select(Series).where(Series.comicvine_id == comicvine_id))
        if existing is not None:
            return existing

        series = Series(
            title=str(series_meta["title"]),
            sort_title=str(series_meta["title"]).lower(),
            year_start=int(series_meta["year_start"]),
            comicvine_id=comicvine_id,
        )
        session.add(series)
        await session.flush()

        for summary in issue_summaries:
            session.add(
                Issue(
                    series_id=series.id,
                    issue_number=float(summary["issue_number"]),
                    comicvine_id=int(summary["comicvine_id"]),
                    title=str(summary["title"]),
                    status=IssueStatus.WANTED,
                )
            )
        await session.flush()
        return series

    async def add_from_comicvine(
        self,
        session: AsyncSession,
        *,
        comicvine_id: int,
        library_root_id: int | None,
        search_on_add: bool,
    ) -> Series:
        series_meta, issue_summaries = await self.prefetch_comicvine_bundle(comicvine_id)
        return await self.add_from_comicvine_prefetched(
            session,
            comicvine_id=comicvine_id,
            library_root_id=library_root_id,
            search_on_add=search_on_add,
            series_meta=series_meta,
            issue_summaries=issue_summaries,
        )


def _write_cbz(path: Path, *, issue_number: int) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"page_{issue_number:03d}.jpg",
            b"\xff\xd8\xff\xd9",
        )
        archive.writestr(
            "ComicInfo.xml",
            (
                "<ComicInfo>"
                "<Series>Benchmark Source</Series>"
                f"<Number>{issue_number}</Number>"
                "</ComicInfo>"
            ),
        )


def _write_cb7(path: Path, *, issue_number: int) -> None:
    import py7zr

    temp_page = path.parent / f"page_{issue_number:03d}.jpg"
    temp_page.write_bytes(b"\xff\xd8\xff\xd9")
    try:
        with py7zr.SevenZipFile(path, "w") as archive:
            archive.write(temp_page, temp_page.name)
    finally:
        temp_page.unlink(missing_ok=True)


def _source_extension_for_index(index: int, *, profile: str) -> str:
    if profile == "synthetic":
        return "cbz"
    return ("cbz", "cbr", "cb7")[(index - 1) % 3]


def _write_source_file(path: Path, *, issue_number: int, profile: str) -> None:
    if profile == "synthetic":
        path.write_bytes(b"benchmark-cbz")
        return
    if path.suffix == ".cb7":
        _write_cb7(path, issue_number=issue_number)
        return
    # `.cbr` inputs are intentionally valid ZIP payloads. The converter detects
    # by magic bytes, which mirrors the mislabeled archive cases Pullbox handles.
    _write_cbz(path, issue_number=issue_number)


def _build_import_tree(
    root: Path,
    *,
    series_count: int,
    files_per_series: int,
    profile: str,
) -> list[list[Path]]:
    series_files: list[list[Path]] = []
    for series_idx in range(series_count):
        folder = root / f"Series {series_idx:04d} (2020)"
        folder.mkdir(parents=True, exist_ok=True)
        file_paths: list[Path] = []
        for issue_idx in range(1, files_per_series + 1):
            extension = _source_extension_for_index(issue_idx, profile=profile)
            path = folder / f"Series {series_idx:04d} #{issue_idx:03d}.{extension}"
            _write_source_file(path, issue_number=issue_idx, profile=profile)
            file_paths.append(path)
        series_files.append(file_paths)
    return series_files


def _heavy_touch_ingest_policy() -> LibraryIngestPolicy:
    return LibraryIngestPolicy(
        rename_on_import=True,
        series_folder_template="{Series} ({Year})",
        comic_file_template="{Series} ({Year}) #{Issue:03d}",
        annual_file_template="{Series} ({Year}) Annual #{Issue:03d}",
        non_standard_file_template="{Series} ({Year}) {Type} {Volume:02d} - {Title}",
        single_non_standard_file_template="{Series} ({Year}) {Type} - {Title}",
        replace_illegal_characters=True,
        colon_replacement="dash",
        post_processing_method="copy",
        torrent_import_strategy="standard",
        normalize_imported_archives_to_cbz=True,
        skip_existing_files=False,
        update_embedded_comicinfo_from_match=True,
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--series-count", type=int, default=40)
    parser.add_argument("--files-per-series", type=int, default=4)
    parser.add_argument(
        "--file-work-profile",
        choices=("synthetic", "mixed-small"),
        default="synthetic",
        help=(
            "synthetic keeps the historical orchestration-only benchmark; "
            "mixed-small uses real registration, archive conversion, rename, "
            "and ComicInfo writes on small valid archives."
        ),
    )
    parser.add_argument(
        "--report-sample-limit",
        type=_bounded_sample_limit,
        default=20,
        help="Maximum library filenames included in the bounded JSON report (0-100).",
    )
    args = parser.parse_args()

    fake_series_service = FakeSeriesService(args.files_per_series)
    service = ImportService(
        series_service=cast("Any", fake_series_service),
        metadata_service=cast("Any", SimpleNamespace(_provider=SimpleNamespace())),
        event_bus=cast("Any", SimpleNamespace()),
    )
    # This profile measures deterministic transformation and registration work.
    # Concurrent SQLite writers are covered by the contention test suite.
    service._settings = service._settings.model_copy(update={"import_file_worker_count": 1})

    register_calls = 0

    async def fake_register_file(
        session: AsyncSession,
        source_path: Path,
        issue: Issue,
        confidence: MatchConfidence,
        **_kwargs: Any,
    ) -> LibraryFile:
        nonlocal register_calls
        register_calls += 1
        root = await session.scalar(select(LibraryRoot).limit(1))
        stat = source_path.stat()
        library_file = LibraryFile(
            file_path=str(source_path),
            file_name=source_path.name,
            file_size=stat.st_size,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            match_confidence=confidence,
            issue_id=issue.id,
            library_root_id=root.id if root is not None else 1,
        )
        session.add(library_file)
        await session.flush()
        return library_file

    with tempfile.TemporaryDirectory(
        prefix="pullbox-import-bench-",
        ignore_cleanup_errors=True,
    ) as tmp:
        temp_root = Path(tmp)
        imports_root = temp_root / "imports"
        library_root = temp_root / "library"
        imports_root.mkdir(parents=True, exist_ok=True)
        library_root.mkdir(parents=True, exist_ok=True)
        series_files = _build_import_tree(
            imports_root,
            series_count=args.series_count,
            files_per_series=args.files_per_series,
            profile=args.file_work_profile,
        )

        db_path = temp_root / "benchmark.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            original_commit = session.commit
            commit_count = 0

            async def counted_commit() -> None:
                nonlocal commit_count
                commit_count += 1
                await original_commit()

            session.commit = counted_commit  # type: ignore[method-assign]

            root = LibraryRoot(name="Benchmark", path=str(library_root), enabled=True)
            session.add(root)
            session.add(
                SystemConfig(
                    key="utility_trash_folder",
                    value=str(temp_root / ".trash"),
                    value_type="string",
                )
            )
            await session.flush()

            real_file_work = args.file_work_profile != "synthetic"
            ingest_policy = _heavy_touch_ingest_policy() if real_file_work else None
            job = ImportJob(
                source_path=str(imports_root),
                source_type=ImportSourceType.FILESYSTEM,
                status=ImportJobStatus.IMPORTING,
                move_to_library=real_file_work is True,
                transfer_method="copy" if real_file_work else "move",
                effective_transfer_method="copy" if real_file_work else "move",
                convert_to_preferred_format=real_file_work,
                update_embedded_comicinfo_from_match=real_file_work,
                ingest_policy_snapshot=(
                    serialize_library_ingest_policy(ingest_policy)
                    if ingest_policy is not None
                    else {}
                ),
                target_library_root_id=root.id,
            )
            session.add(job)
            await session.flush()

            for series_idx, file_paths in enumerate(series_files):
                cv_id = 200000 + series_idx
                imported_series = ImportedSeries(
                    import_job_id=job.id,
                    raw_series_name=f"Series {series_idx:04d}",
                    raw_year=2020,
                    status=ImportSeriesStatus.CONFIRMED,
                    cv_id=cv_id,
                    file_count=len(file_paths),
                )
                session.add(imported_series)
                await session.flush()

                for issue_idx, path in enumerate(file_paths, start=1):
                    session.add(
                        ImportedFile(
                            import_job_id=job.id,
                            import_series_id=imported_series.id,
                            file_path=str(path),
                            file_name=path.name,
                            file_size=path.stat().st_size,
                            file_format=path.suffix.lower().lstrip("."),
                            parsed_series=f"Series {series_idx:04d}",
                            parsed_issue_number=float(issue_idx),
                            status=ImportedFileStatus.CONFIRMED,
                            matched_issue_cv_id=cv_id * 1000 + issue_idx,
                            match_confidence="high",
                            match_method="benchmark",
                        )
                    )
            await session.commit()

            import pullbox.services.import_service as import_service_module
            from pullbox.services import operation_progress_dispatch

            original_register = import_service_module.register_library_file
            original_progress_session_factory = operation_progress_dispatch.get_session_factory
            operation_progress_dispatch.get_session_factory = cast(
                "Any",
                lambda: session_factory,
            )
            if not real_file_work:
                import_service_module.register_library_file = cast("Any", fake_register_file)
            try:
                started_at = time.monotonic()
                await service.run_import(session, job.id)
                elapsed_ms = round((time.monotonic() - started_at) * 1000)
            finally:
                try:
                    await operation_progress_dispatch.drain_operation_progress_updates()
                finally:
                    import_service_module.register_library_file = original_register
                    operation_progress_dispatch.get_session_factory = (
                        original_progress_session_factory
                    )

            from pullbox.services.import_comicinfo_enrichment import comicinfo_enrichment_tasks

            pending_enrichment = list(comicinfo_enrichment_tasks)
            if pending_enrichment:
                await asyncio.gather(*pending_enrichment, return_exceptions=True)

            await session.refresh(job)
            library_file_summary = await _summarize_library_files(
                session,
                sample_limit=args.report_sample_limit,
            )
            operation_progress_count = int(
                await session.scalar(select(func.count(OperationProgress.id))) or 0
            )
            source_format_counts = Counter(
                path.suffix.lower().lstrip(".")
                for file_paths in series_files
                for path in file_paths
            )
            report = {
                "file_work_profile": args.file_work_profile,
                "real_file_work": real_file_work,
                "series_count": args.series_count,
                "files_per_series": args.files_per_series,
                "elapsed_ms": elapsed_ms,
                "commit_count": commit_count,
                "prefetch_calls": fake_series_service.prefetch_calls,
                "series_add_calls": fake_series_service.add_calls,
                "register_calls": register_calls,
                "operation_progress_count": operation_progress_count,
                **library_file_summary,
                "source_format_counts": dict(sorted(source_format_counts.items())),
                "peak_rss_bytes": current_process_peak_rss_bytes(),
                "final_status": job.status.value,
                "series_imported": job.series_imported,
                "series_failed": job.series_failed,
                "total_files_imported": job.total_files_imported,
                "total_files_failed": job.total_files_failed,
            }
            print(json.dumps(report, indent=2, sort_keys=True))

        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
