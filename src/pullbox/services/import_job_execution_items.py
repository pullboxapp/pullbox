"""Per-review-group execution helpers for Step 4 imports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select as sa_select

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportFileHandlingMode,
    ImportJob,
    ImportSeriesStatus,
)
from pullbox.models.series import Series
from pullbox.providers.base import IssueSummary
from pullbox.services.import_catalog_hydration import schedule_catalog_hydration
from pullbox.services.import_file_resolution import load_importable_files
from pullbox.services.import_job_actions import (
    build_series_cover_cache_action_payload,
    build_series_cover_path_updated_action_payload,
    build_series_created_action_payload,
    build_series_folder_created_action_payload,
    build_series_monitoring_updated_action_payload,
)
from pullbox.services.import_job_execution_progress import progress_session_factory_for_runtime
from pullbox.services.import_split_series import apply_import_preferred_series_root

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Protocol

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.services.import_job_execution_types import (
        LogEventFunc,
        ProcessSeriesFilesFunc,
        RecordActionFunc,
        ReportFileProgressFunc,
        SeriesServiceFunc,
    )

    class ScheduleCatalogHydrationFunc(Protocol):
        def __call__(
            self,
            session: AsyncSession,
            *,
            series_service: SeriesServiceFunc,
            series_id: int,
            search_on_add: bool,
        ) -> None: ...


async def execute_new_series(
    session: AsyncSession,
    job: ImportJob,
    item: ImportedSeries,
    *,
    imported_count: int,
    failed_count: int,
    prefetched_bundle: tuple[Any, list[Any]] | None = None,
    series_service: SeriesServiceFunc,
    process_series_files: ProcessSeriesFilesFunc,
    record_action: RecordActionFunc,
    log_event: LogEventFunc,
    report_file_progress: ReportFileProgressFunc | None = None,
    queue_catalog_hydration: Callable[[int, bool], None] | None = None,
    schedule_catalog_hydration_func: ScheduleCatalogHydrationFunc | None = None,
) -> tuple[int, int, int, int, bool]:
    """Create/reuse a local series and import the selected files for one review row."""
    item_id = item.id
    cv_id = item.user_selected_cv_id or item.cv_id
    if cv_id is None:
        item.status = ImportSeriesStatus.FAILED
        item.error_message = "No ComicVine ID available"
        await session.commit()
        return 0, 0, imported_count, failed_count + 1, False

    importable_files = await load_importable_files(session, item)
    if not importable_files:
        item.status = ImportSeriesStatus.FAILED
        item.error_message = "No eligible files available for import"
        failed_count += 1
        await log_event(
            session,
            job.id,
            "ERROR",
            "import_series_no_eligible_files",
            message=f"No eligible files available to import for {item.raw_series_name}",
            raw_series_name=item.raw_series_name,
            cv_id=cv_id,
            series_id=None,
        )
        await session.flush()
        return 0, 0, imported_count, failed_count, True

    # Keep the import-review row clean while provider fetches and file placement
    # are running. That prevents autoflush from opening a SQLite write
    # transaction before the slow work is finished.
    with session.no_autoflush:
        current_library_root_id = (
            None
            if job.file_handling_mode == ImportFileHandlingMode.IN_PLACE
            else job.target_library_root_id
        )
        existing_series_row = (
            await session.execute(
                sa_select(
                    Series.id,
                    Series.cover_path,
                    Series.path,
                    Series.library_root_id,
                    Series.preferred_library_root_id,
                    Series.monitored,
                ).where(Series.comicvine_id == cv_id)
            )
        ).one_or_none()
        existing_series_id = existing_series_row[0] if existing_series_row is not None else None
        existing_series_cover_path = (
            existing_series_row[1] if existing_series_row is not None else None
        )
        existing_series_path = existing_series_row[2] if existing_series_row is not None else None
        existing_series_library_root_id = (
            existing_series_row[3] if existing_series_row is not None else None
        )
        existing_series_preferred_root_id = (
            existing_series_row[4] if existing_series_row is not None else None
        )
        existing_series_monitored = (
            bool(existing_series_row[5]) if existing_series_row is not None else False
        )
        targeted_descriptor = getattr(
            type(series_service),
            "add_from_import_review_targeted",
            None,
        )
        add_prefetched_descriptor = getattr(
            type(series_service),
            "add_from_comicvine_prefetched",
            None,
        )
        if targeted_descriptor is not None:
            add_from_import_review_targeted = targeted_descriptor.__get__(
                series_service,
                type(series_service),
            )
            new_series = await add_from_import_review_targeted(
                session,
                import_series=item,
                library_root_id=current_library_root_id,
                search_on_add=job.search_on_add,
                issue_summaries=targeted_issue_summaries_for_import_files(importable_files),
            )
        elif prefetched_bundle is not None and add_prefetched_descriptor is not None:
            add_from_comicvine_prefetched = add_prefetched_descriptor.__get__(
                series_service,
                type(series_service),
            )
            series_meta, issue_summaries = prefetched_bundle
            new_series = await add_from_comicvine_prefetched(
                session,
                comicvine_id=cv_id,
                library_root_id=current_library_root_id,
                search_on_add=job.search_on_add,
                series_meta=series_meta,
                issue_summaries=issue_summaries,
            )
        else:
            new_series = await series_service.add_from_comicvine(
                session,
                comicvine_id=cv_id,
                library_root_id=current_library_root_id,
                search_on_add=job.search_on_add,
            )
    new_series_id = new_series.id
    # Persist the review-row ownership link before file work begins. A
    # cooperative cancellation can interrupt that work, and the rollback
    # journal must still be able to prove which import created this series.
    item.series_id = new_series_id

    await apply_import_preferred_series_root(
        session,
        job,
        series_id=new_series_id,
        record_action=record_action,
    )
    if existing_series_id is None:
        await record_action(
            session,
            job,
            phase="import",
            action_type="series_created",
            payload=await build_series_created_action_payload(
                session,
                series_id=new_series_id,
                import_series_id=item_id,
            ),
        )
    else:
        monitoring_action_payload = await build_series_monitoring_updated_action_payload(
            session,
            series_id=new_series_id,
            import_series_id=item_id,
            previous_monitored=existing_series_monitored,
        )
        if monitoring_action_payload is not None:
            await record_action(
                session,
                job,
                phase="import",
                action_type="series_monitoring_updated",
                payload=monitoring_action_payload,
            )
        folder_action_payload = await build_series_folder_created_action_payload(
            session,
            series_id=new_series_id,
            import_series_id=item_id,
            previous_series_path=existing_series_path,
            previous_library_root_id=existing_series_library_root_id,
            previous_preferred_library_root_id=existing_series_preferred_root_id,
        )
        if folder_action_payload is not None:
            await record_action(
                session,
                job,
                phase="import",
                action_type="series_folder_created",
                payload=folder_action_payload,
            )
        cover_action_payload = await build_series_cover_cache_action_payload(
            session,
            series_id=new_series_id,
            import_series_id=item_id,
            previous_cover_path=existing_series_cover_path,
        )
        if cover_action_payload is not None:
            await record_action(
                session,
                job,
                phase="import",
                action_type="series_cover_cache_created",
                payload=cover_action_payload,
            )
        else:
            cover_path_action_payload = await build_series_cover_path_updated_action_payload(
                session,
                series_id=new_series_id,
                import_series_id=item_id,
                previous_cover_path=existing_series_cover_path,
            )
            if cover_path_action_payload is not None:
                await record_action(
                    session,
                    job,
                    phase="import",
                    action_type="series_cover_path_updated",
                    payload=cover_path_action_payload,
                )

    await log_event(
        session,
        job.id,
        "INFO",
        "import_series_added",
        message=f"Prepared series: {item.raw_series_name}",
        raw_series_name=item.raw_series_name,
        cv_id=cv_id,
        series_id=new_series_id,
    )
    # Release the series/action/log write transaction before long-running file
    # preparation and conversion start so pause/cancel requests can persist
    # immediately on the hot import job row.
    await session.commit()
    search_on_add = bool(job.search_on_add)

    def queue_or_schedule_catalog_hydration() -> None:
        if queue_catalog_hydration is not None:
            queue_catalog_hydration(new_series_id, search_on_add)
            return
        scheduler = schedule_catalog_hydration_func or schedule_catalog_hydration_for_series
        scheduler(
            session,
            series_service=series_service,
            series_id=new_series_id,
            search_on_add=search_on_add,
        )

    files_ok, files_err = await process_series_files(
        session,
        job,
        item,
        series_id_override=new_series_id,
        report_file_progress=report_file_progress,
    )
    reloaded_item = await session.get(ImportedSeries, item_id)
    if reloaded_item is not None:
        item = reloaded_item
    if files_ok == 0 and files_err == 0:
        if await has_safety_blocked_files(session, item_id):
            item.status = ImportSeriesStatus.IMPORTED
            item.series_id = new_series_id
            item.error_message = None
            imported_count += 1
            queue_or_schedule_catalog_hydration()
            await session.flush()
            return files_ok, files_err, imported_count, failed_count, True
        item.status = ImportSeriesStatus.FAILED
        item.error_message = "No eligible files available for import"
        failed_count += 1
        await log_event(
            session,
            job.id,
            "ERROR",
            "import_series_no_eligible_files",
            message=f"No eligible files available to import for {item.raw_series_name}",
            raw_series_name=item.raw_series_name,
            cv_id=cv_id,
            series_id=new_series_id,
        )
        await session.flush()
        return files_ok, files_err, imported_count, failed_count, True
    item.status = ImportSeriesStatus.IMPORTED
    item.series_id = new_series_id
    item.error_message = None
    imported_count += 1
    queue_or_schedule_catalog_hydration()
    return files_ok, files_err, imported_count, failed_count, True


async def has_safety_blocked_files(session: AsyncSession, imported_series_id: int) -> bool:
    """Return whether a review row has deferred resource-safety file decisions."""
    safety_file_id = await session.scalar(
        sa_select(ImportedFile.id)
        .where(
            ImportedFile.import_series_id == imported_series_id,
            ImportedFile.status == ImportedFileStatus.SAFETY_BLOCKED,
        )
        .limit(1)
    )
    return safety_file_id is not None


def targeted_issue_summaries_for_import_files(files: list[ImportedFile]) -> list[IssueSummary]:
    """Build Step 4 issue summaries from review-time file matches."""
    summaries: list[IssueSummary] = []
    seen_provider_ids: set[str] = set()
    seen_numbers: set[float] = set()
    for imp_file in files:
        diagnostics = imp_file.diagnostics if isinstance(imp_file.diagnostics, dict) else {}
        summary_payload = diagnostics.get("target_issue_summary")
        issue_number: float | None
        if not isinstance(summary_payload, dict):
            if imp_file.matched_issue_cv_id is not None and imp_file.matched_issue_id is None:
                raise ValueError("Import file is missing required target_issue_summary diagnostics")
            continue

        provider_id = str(summary_payload.get("provider_id") or "").strip()
        try:
            issue_number = float(summary_payload["issue_number"])
        except (KeyError, TypeError, ValueError):
            issue_number = imp_file.parsed_issue_number
        title = summary_payload.get("title")
        release_date = summary_payload.get("release_date")
        cover_url = summary_payload.get("cover_url")
        issue_type = str(summary_payload["issue_type"])

        if not provider_id or issue_number is None:
            continue
        number_key = float(issue_number)
        if provider_id in seen_provider_ids or number_key in seen_numbers:
            continue
        seen_provider_ids.add(provider_id)
        seen_numbers.add(number_key)
        summaries.append(
            IssueSummary(
                provider_id=provider_id,
                issue_number=number_key,
                title=str(title) if title else None,
                release_date=str(release_date) if release_date else None,
                cover_url=str(cover_url) if cover_url else None,
                issue_type=issue_type,
            )
        )
    return summaries


def schedule_catalog_hydration_for_series(
    session: AsyncSession,
    *,
    series_service: SeriesServiceFunc,
    series_id: int,
    search_on_add: bool,
) -> None:
    """Schedule full ComicVine issue-list hydration after fast targeted import."""
    schedule_catalog_hydration(
        progress_session_factory_for_runtime(session),
        series_service=series_service,
        series_id=series_id,
        search_on_add=search_on_add,
    )


async def execute_duplicate_series_merge(
    session: AsyncSession,
    job: ImportJob,
    item: ImportedSeries,
    *,
    process_series_files: ProcessSeriesFilesFunc,
    record_action: RecordActionFunc,
    log_event: LogEventFunc,
    report_file_progress: ReportFileProgressFunc | None = None,
) -> tuple[int, int, bool]:
    """Import selected files into an existing duplicate-series target."""
    item_id = item.id
    if item.series_id is None:
        await session.commit()
        return 0, 0, False

    await apply_import_preferred_series_root(
        session,
        job,
        series_id=item.series_id,
        record_action=record_action,
    )

    await log_event(
        session,
        job.id,
        "INFO",
        "import_duplicate_series_merge_started",
        message=f"Merging duplicate-series files into {item.raw_series_name}",
        raw_series_name=item.raw_series_name,
        existing_series_id=item.series_id,
    )
    # Duplicate-file imports can still spend a long time converting or
    # materializing files. Commit the intent log first so control requests do
    # not block behind an unrelated open write transaction.
    await session.commit()
    files_ok, files_err = await process_series_files(
        session,
        job,
        item,
        duplicate_mode=True,
        report_file_progress=report_file_progress,
    )
    reloaded_item = await session.get(ImportedSeries, item_id)
    if reloaded_item is not None:
        item = reloaded_item
    return files_ok, files_err, True
