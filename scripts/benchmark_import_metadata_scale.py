"""Exercise import review/rollback at metadata scale without archive payloads.

The default profile represents 50,000 series and 200,000 files entirely as
database staging rows. It also confirms 10,000 staged story arcs, then restores
all review state through the rollback path. No provider or filesystem scanner
is constructed, so both call counts are structurally zero.

Usage:
  .venv/bin/python scripts/benchmark_import_metadata_scale.py
  .venv/bin/python scripts/benchmark_import_metadata_scale.py \
    --series-count 100 --files-per-series 4 --story-arc-count 100
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

from sqlalchemy import event, func, insert, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from pullbox.models import Base
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.story_arc import (
    ImportedStoryArcStatus,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.performance.baseline import current_process_peak_rss_bytes
from pullbox.services.import_rollback_state import restore_review_state_after_rollback
from pullbox.services.import_story_arc_review import confirm_import_story_arcs

_POSTGRESQL_URL_ENV = "PULLBOX_IMPORT_BENCHMARK_POSTGRESQL_URL"


async def _prepare_database(
    connection: AsyncConnection,
    *,
    backend: str,
    reset_dedicated_database: bool,
) -> None:
    """Prepare an empty schema without attempting cyclic table-by-table drops."""
    if reset_dedicated_database:
        if backend != "postgresql":
            raise ValueError("dedicated schema reset requires PostgreSQL")
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
    await connection.run_sync(Base.metadata.create_all)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


async def _seed_metadata(
    session: AsyncSession,
    *,
    job_id: int,
    series_count: int,
    files_per_series: int,
    story_arc_count: int,
    insert_batch_size: int,
) -> int:
    commits = 0
    for start in range(1, series_count + 1, insert_batch_size):
        stop = min(start + insert_batch_size, series_count + 1)
        await session.execute(
            insert(ImportedSeries),
            [
                {
                    "id": series_id,
                    "import_job_id": job_id,
                    "raw_series_name": f"Synthetic Series {series_id}",
                    "file_count": files_per_series,
                    "status": ImportSeriesStatus.IMPORTED,
                    "files_imported": files_per_series,
                }
                for series_id in range(start, stop)
            ],
        )
        await session.commit()
        commits += 1

    total_files = series_count * files_per_series
    for start in range(1, total_files + 1, insert_batch_size):
        stop = min(start + insert_batch_size, total_files + 1)
        await session.execute(
            insert(ImportedFile),
            [
                {
                    "id": file_id,
                    "import_job_id": job_id,
                    "import_series_id": ((file_id - 1) // files_per_series) + 1,
                    "file_path": f"/metadata-only/{file_id}.cbz",
                    "file_name": f"{file_id}.cbz",
                    "file_size": 0,
                    "file_format": "cbz",
                    "status": ImportedFileStatus.IMPORTED,
                    "include_in_import": True,
                }
                for file_id in range(start, stop)
            ],
        )
        await session.commit()
        commits += 1

    for start in range(1, story_arc_count + 1, insert_batch_size):
        stop = min(start + insert_batch_size, story_arc_count + 1)
        await session.execute(
            insert(ImportedStoryArc),
            [
                {
                    "id": arc_id,
                    "import_job_id": job_id,
                    "source_kind": StoryArcSourceKind.MYLAR3,
                    "source_key": f"metadata-scale:{arc_id}",
                    "source_arc_id": f"source-{arc_id}",
                    "source_ordinal": arc_id,
                    "name": f"Synthetic Arc {arc_id}",
                    "status": ImportedStoryArcStatus.READY,
                    "selected_for_import": True,
                }
                for arc_id in range(start, stop)
            ],
        )
        await session.execute(
            insert(ImportedStoryArcEntry),
            [
                {
                    "id": arc_id,
                    "imported_story_arc_id": arc_id,
                    "source_ordinal": 1,
                    "reading_order": arc_id,
                    "reading_order_raw": str(arc_id),
                    "resolution_state": StoryArcResolutionState.MISSING,
                    "source_kind": StoryArcSourceKind.MYLAR3,
                    "source_entry_id": f"entry-{arc_id}",
                    "source_arc_id": f"source-{arc_id}",
                    "source_issue_number_text": "1AU",
                    "selected_for_import": True,
                }
                for arc_id in range(start, stop)
            ],
        )
        await session.commit()
        commits += 1
    return commits


def _install_select_counter(engine: AsyncEngine, counts: dict[str, int], phase: list[str]) -> None:
    def record_statement(*args: object) -> None:
        if str(args[2]).lstrip().upper().startswith("SELECT"):
            counts[phase[0]] = counts.get(phase[0], 0) + 1

    event.listen(engine.sync_engine, "before_cursor_execute", record_statement)


async def _run(args: argparse.Namespace) -> dict[str, object]:
    total_started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="pullbox-metadata-scale-") as tmp:
        db_path = Path(tmp) / "metadata-scale.db"
        if args.backend == "postgresql":
            db_url = os.environ.get(_POSTGRESQL_URL_ENV, "")
            if not db_url.startswith("postgresql+asyncpg://"):
                raise RuntimeError(
                    f"{_POSTGRESQL_URL_ENV} must contain a dedicated PostgreSQL async URL"
                )
        else:
            db_url = f"sqlite+aiosqlite:///{db_path}"
        engine = create_async_engine(db_url)
        async with engine.begin() as connection:
            await _prepare_database(
                connection,
                backend=args.backend,
                reset_dedicated_database=args.reset_dedicated_database,
            )

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        select_counts: dict[str, int] = {}
        phase = ["idle"]
        _install_select_counter(engine, select_counts, phase)
        async with session_factory() as session:
            job = ImportJob(
                source_path="/metadata-only",
                source_type=ImportSourceType.MYLAR3,
                status=ImportJobStatus.REVIEW,
            )
            session.add(job)
            await session.commit()

            seed_started = time.monotonic()
            seed_commit_count = await _seed_metadata(
                session,
                job_id=int(job.id),
                series_count=args.series_count,
                files_per_series=args.files_per_series,
                story_arc_count=args.story_arc_count,
                insert_batch_size=args.insert_batch_size,
            )
            seed_elapsed_ms = round((time.monotonic() - seed_started) * 1000)

            phase[0] = "confirm"
            confirm_started = time.monotonic()
            confirmed_arc_count = await confirm_import_story_arcs(
                session,
                int(job.id),
                story_arc_ids=(),
                decisions=(),
            )
            await session.commit()
            confirm_elapsed_ms = round((time.monotonic() - confirm_started) * 1000)
            session.expunge_all()

            await session.execute(
                update(ImportedStoryArc)
                .where(ImportedStoryArc.import_job_id == job.id)
                .values(status=ImportedStoryArcStatus.IMPORTED)
            )
            await session.execute(
                update(ImportJob)
                .where(ImportJob.id == job.id)
                .values(status=ImportJobStatus.ROLLING_BACK)
            )
            await session.commit()

            phase[0] = "rollback"
            rollback_started = time.monotonic()
            await restore_review_state_after_rollback(
                session,
                int(job.id),
                batch_size=args.operation_batch_size,
            )
            await session.commit()
            rollback_elapsed_ms = round((time.monotonic() - rollback_started) * 1000)
            phase[0] = "report"

            final_series = await session.scalar(
                select(func.count())
                .select_from(ImportedSeries)
                .where(ImportedSeries.status == ImportSeriesStatus.MATCHED)
            )
            final_files = await session.scalar(
                select(func.count())
                .select_from(ImportedFile)
                .where(ImportedFile.status == ImportedFileStatus.NO_MATCH)
            )
            final_arcs = await session.scalar(
                select(func.count())
                .select_from(ImportedStoryArc)
                .where(ImportedStoryArc.status == ImportedStoryArcStatus.CONFIRMED)
            )
            if args.backend == "postgresql":
                database_bytes = int(
                    await session.scalar(text("SELECT pg_database_size(current_database())")) or 0
                )
            else:
                database_bytes = db_path.stat().st_size

        await engine.dispose()
        return {
            "profile": "metadata_only",
            "backend": args.backend,
            "series_count": args.series_count,
            "files_per_series": args.files_per_series,
            "represented_file_count": args.series_count * args.files_per_series,
            "story_arc_count": args.story_arc_count,
            "archive_payload_count": 0,
            "provider_call_count": 0,
            "filesystem_scan_count": 0,
            "insert_batch_size": args.insert_batch_size,
            "operation_batch_size": args.operation_batch_size,
            "seed_commit_count": seed_commit_count,
            "confirmed_arc_count": confirmed_arc_count,
            "final_matched_series_count": int(final_series or 0),
            "final_no_match_file_count": int(final_files or 0),
            "final_confirmed_arc_count": int(final_arcs or 0),
            "seed_elapsed_ms": seed_elapsed_ms,
            "confirm_elapsed_ms": confirm_elapsed_ms,
            "rollback_elapsed_ms": rollback_elapsed_ms,
            "total_elapsed_ms": round((time.monotonic() - total_started) * 1000),
            "confirm_select_count": select_counts.get("confirm", 0),
            "rollback_select_count": select_counts.get("rollback", 0),
            "peak_rss_bytes": current_process_peak_rss_bytes(),
            "database_bytes": database_bytes,
        }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("sqlite", "postgresql"), default="sqlite")
    parser.add_argument(
        "--reset-dedicated-database",
        action="store_true",
        help="Drop and recreate all tables; only safe for a dedicated benchmark database",
    )
    parser.add_argument("--series-count", type=_positive_int, default=50_000)
    parser.add_argument("--files-per-series", type=_positive_int, default=4)
    parser.add_argument("--story-arc-count", type=_positive_int, default=10_000)
    parser.add_argument("--insert-batch-size", type=_positive_int, default=2_000)
    parser.add_argument("--operation-batch-size", type=_positive_int, default=500)
    args = parser.parse_args()
    if args.reset_dedicated_database and args.backend != "postgresql":
        parser.error("--reset-dedicated-database requires --backend postgresql")
    print(json.dumps(await _run(args), indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
