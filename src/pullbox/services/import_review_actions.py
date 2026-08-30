"""Import review mutation helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime

from sqlalchemy import or_
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.core.name_matcher import NameMatcher
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.services.import_duplicates import duplicate_merge_is_actionable, is_duplicate_series
from pullbox.services.import_safety_diagnostics import normalize_import_safety_diagnostics

RecomputeFileCounters = Callable[[AsyncSession, ImportJob, list[int]], Awaitable[None]]
RecomputeSeriesCounters = Callable[[AsyncSession, ImportJob], Awaitable[None]]


async def resolve_conflict(
    session: AsyncSession,
    job_id: int,
    group_id: int,
    chosen_file_id: int,
    *,
    recompute_file_counters: RecomputeFileCounters,
) -> list[ImportedFile]:
    """Resolve a conflict group by choosing one file."""
    _resolved_group_ids, _resolved_series_ids, files_by_group = await resolve_conflicts(
        session,
        job_id,
        [(group_id, chosen_file_id)],
        recompute_file_counters=recompute_file_counters,
    )
    return files_by_group[group_id]


async def resolve_conflicts(
    session: AsyncSession,
    job_id: int,
    resolutions: list[tuple[int, int]],
    *,
    recompute_file_counters: RecomputeFileCounters,
) -> tuple[list[int], list[int], dict[int, list[ImportedFile]]]:
    """Resolve multiple conflict groups in one review mutation."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if job.status != ImportJobStatus.REVIEW:
        raise ValidationError("Job must be in REVIEW state to resolve conflicts")

    normalized_resolutions: list[tuple[int, int]] = []
    seen_group_ids: set[int] = set()
    for group_id, chosen_file_id in resolutions:
        normalized_group_id = int(group_id)
        normalized_chosen_file_id = int(chosen_file_id)
        if normalized_group_id in seen_group_ids:
            raise ValidationError(
                f"Conflict group {normalized_group_id} was provided more than once"
            )
        seen_group_ids.add(normalized_group_id)
        normalized_resolutions.append((normalized_group_id, normalized_chosen_file_id))

    if not normalized_resolutions:
        raise ValidationError("At least one conflict resolution is required")

    group_ids = [group_id for group_id, _chosen_file_id in normalized_resolutions]
    query = sa_select(ImportedFile).where(
        ImportedFile.import_job_id == job_id,
        ImportedFile.conflict_group_id.in_(group_ids),
        ImportedFile.status.in_(
            [
                ImportedFileStatus.CONFLICT,
                ImportedFileStatus.CONFIRMED,
                ImportedFileStatus.SKIPPED,
            ]
        ),
    )
    result = await session.execute(query)
    files_by_group: dict[int, list[ImportedFile]] = {}
    for imp_file in result.scalars().all():
        if imp_file.conflict_group_id is None:
            continue
        files_by_group.setdefault(imp_file.conflict_group_id, []).append(imp_file)

    affected_series_ids: set[int] = set()
    resolved_group_ids: list[int] = []
    for normalized_group_id, normalized_chosen_file_id in normalized_resolutions:
        group_files = files_by_group.get(normalized_group_id)
        if not group_files:
            raise NotFoundError("ConflictGroup", normalized_group_id)

        file_ids_in_group = {imp_file.id for imp_file in group_files}
        if normalized_chosen_file_id not in file_ids_in_group:
            raise ValidationError(
                f"File {normalized_chosen_file_id} is not part of conflict group "
                f"{normalized_group_id}"
            )

        for imp_file in group_files:
            if imp_file.id == normalized_chosen_file_id:
                imp_file.status = ImportedFileStatus.CONFIRMED
                imp_file.is_preferred = True
                imp_file.include_in_import = True
            else:
                imp_file.status = ImportedFileStatus.SKIPPED
                imp_file.is_preferred = False
                imp_file.include_in_import = False
            affected_series_ids.add(imp_file.import_series_id)

        resolved_group_ids.append(normalized_group_id)

    affected_series_id_list = sorted(affected_series_ids)
    await recompute_file_counters(session, job, affected_series_id_list)
    if affected_series_id_list:
        affected_series = list(
            (
                await session.execute(
                    sa_select(ImportedSeries).where(
                        ImportedSeries.id.in_(affected_series_id_list),
                    )
                )
            )
            .scalars()
            .all()
        )
        for imported_series in affected_series:
            if imported_series.status == ImportSeriesStatus.MATCHED:
                imported_series.selected_for_import = (imported_series.files_conflict or 0) == 0
    return resolved_group_ids, affected_series_id_list, files_by_group


async def reset_conflicts(
    session: AsyncSession,
    job_id: int,
    group_ids: list[int],
    *,
    recompute_file_counters: RecomputeFileCounters,
) -> tuple[list[int], list[int]]:
    """Reset resolved conflict groups back to CONFLICT review state."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if job.status != ImportJobStatus.REVIEW:
        raise ValidationError("Job must be in REVIEW state to reset conflicts")

    normalized_group_ids = sorted({int(group_id) for group_id in group_ids if int(group_id) > 0})
    filters = [
        ImportedFile.import_job_id == job_id,
        ImportedFile.conflict_group_id.is_not(None),
        ImportedFile.status.in_(
            [
                ImportedFileStatus.CONFLICT,
                ImportedFileStatus.CONFIRMED,
                ImportedFileStatus.SKIPPED,
            ]
        ),
    ]
    if normalized_group_ids:
        filters.append(ImportedFile.conflict_group_id.in_(normalized_group_ids))

    query = sa_select(ImportedFile).where(*filters)
    result = await session.execute(query)
    files = list(result.scalars().all())
    if not files:
        raise ValidationError("No resettable conflict groups were found for this import job")

    files_by_group: dict[int, list[ImportedFile]] = {}
    for imp_file in files:
        group_id = imp_file.conflict_group_id
        if group_id is None:
            continue
        files_by_group.setdefault(group_id, []).append(imp_file)

    if not normalized_group_ids:
        normalized_group_ids = sorted(files_by_group.keys())

    affected_series_ids: set[int] = set()
    reset_group_ids: list[int] = []
    for group_id in normalized_group_ids:
        group_files = files_by_group.get(group_id)
        if not group_files:
            continue

        preferred_file_id: int | None = None
        for imp_file in group_files:
            preferred_value = (imp_file.diagnostics or {}).get("preferred_file_id")
            if isinstance(preferred_value, int):
                preferred_file_id = preferred_value
                break
        if preferred_file_id is None:
            preferred_file_id = next(
                (imp_file.id for imp_file in group_files if imp_file.is_preferred),
                group_files[0].id,
            )

        for imp_file in group_files:
            imp_file.status = ImportedFileStatus.CONFLICT
            imp_file.include_in_import = False
            imp_file.is_preferred = imp_file.id == preferred_file_id
            affected_series_ids.add(imp_file.import_series_id)

        reset_group_ids.append(group_id)

    if not reset_group_ids:
        raise ValidationError("No resettable conflict groups were found for this import job")

    reset_series_ids = sorted(affected_series_ids)
    await recompute_file_counters(session, job, reset_series_ids)
    if reset_series_ids:
        reset_series = list(
            (
                await session.execute(
                    sa_select(ImportedSeries).where(ImportedSeries.id.in_(reset_series_ids))
                )
            )
            .scalars()
            .all()
        )
        for imported_series in reset_series:
            imported_series.selected_for_import = False
    return reset_group_ids, reset_series_ids


async def update_series_selection(
    session: AsyncSession,
    job_id: int,
    imported_series_id: int,
    *,
    include_in_import: bool,
) -> ImportedSeries:
    """Persist include/exclude state for a matched import-review series."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if job.status != ImportJobStatus.REVIEW:
        raise ValidationError("Job must be in REVIEW state to update series selection")

    imported_series = await session.get(ImportedSeries, imported_series_id)
    if imported_series is None or imported_series.import_job_id != job_id:
        raise NotFoundError("ImportedSeries", imported_series_id)
    if imported_series.status != ImportSeriesStatus.MATCHED:
        raise ValidationError("Only matched series can be selected for import")
    if (imported_series.files_conflict or 0) > 0:
        raise ValidationError("Resolve file conflicts before selecting this series")
    if include_in_import and (imported_series.files_matched or 0) <= 0:
        raise ValidationError("This series has no importable files to select")

    imported_series.selected_for_import = include_in_import
    await session.flush()
    return imported_series


async def _load_safety_blocked_file(
    session: AsyncSession,
    job_id: int,
    file_id: int,
    *,
    allow_terminal_job: bool = False,
) -> tuple[ImportJob, ImportedSeries, ImportedFile]:
    """Load a safety-blocked review file and its parent row."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if job.status != ImportJobStatus.REVIEW and not (
        allow_terminal_job and job.status in {ImportJobStatus.COMPLETED, ImportJobStatus.FAILED}
    ):
        raise ValidationError("Job must be in REVIEW state to update safety review files")

    imp_file = await session.get(ImportedFile, file_id)
    if imp_file is None or imp_file.import_job_id != job_id:
        raise NotFoundError("ImportedFile", file_id)
    if imp_file.status != ImportedFileStatus.SAFETY_BLOCKED:
        raise ValidationError("Only safety-blocked files can be updated from safety review")

    imported_series = await session.get(ImportedSeries, imp_file.import_series_id)
    if imported_series is None or imported_series.import_job_id != job_id:
        raise NotFoundError("ImportedSeries", imp_file.import_series_id)
    return job, imported_series, imp_file


async def allow_safety_blocked_file_once(
    session: AsyncSession,
    job_id: int,
    file_id: int,
    *,
    recompute_file_counters: RecomputeFileCounters,
    recompute_series_counters: RecomputeSeriesCounters,
    retry_import: bool = False,
) -> ImportedFile:
    """Clear a per-file safety block for this import job without selecting it."""
    job, imported_series, imp_file = await _load_safety_blocked_file(
        session,
        job_id,
        file_id,
        allow_terminal_job=retry_import,
    )

    diagnostics = dict(imp_file.diagnostics or {})
    previous_block = diagnostics.pop("safety_block", None)
    if not isinstance(previous_block, Mapping):
        raise ValidationError("This safety block cannot be overridden.")
    normalized_previous_block = normalize_import_safety_diagnostics(previous_block)
    if normalized_previous_block["overrideable"] is not True:
        raise ValidationError("This safety block cannot be overridden.")
    diagnostics["safety_exception"] = {
        "allowed_once": True,
        "allowed_at": datetime.now(UTC).isoformat(),
        "previous_block": normalized_previous_block,
    }
    imp_file.status = (
        ImportedFileStatus.CONFIRMED if retry_import else ImportedFileStatus.SAFETY_APPROVED
    )
    imp_file.include_in_import = bool(retry_import)
    imp_file.error_message = None
    imp_file.diagnostics = diagnostics
    imported_series.selected_for_import = bool(retry_import)
    if retry_import and imported_series.status in {
        ImportSeriesStatus.IMPORTED,
        ImportSeriesStatus.FAILED,
    }:
        imported_series.status = ImportSeriesStatus.CONFIRMED
    if retry_import:
        imported_series.error_message = None
        job.error_message = None
    if retry_import:
        job.status = ImportJobStatus.IMPORTING

    await recompute_file_counters(session, job, [imported_series.id])
    await recompute_series_counters(session, job)
    await session.flush()
    return imp_file


async def skip_safety_blocked_file(
    session: AsyncSession,
    job_id: int,
    file_id: int,
    *,
    recompute_file_counters: RecomputeFileCounters,
    recompute_series_counters: RecomputeSeriesCounters,
) -> ImportedFile:
    """Skip a safety-blocked file from the active import review."""
    job, imported_series, imp_file = await _load_safety_blocked_file(session, job_id, file_id)

    diagnostics = dict(imp_file.diagnostics or {})
    imp_file.status = ImportedFileStatus.SKIPPED
    imp_file.include_in_import = False
    imp_file.matched_issue_id = None
    imp_file.matched_issue_cv_id = None
    imp_file.match_confidence = None
    imp_file.match_method = "safety_review_skip"
    imp_file.conflict_group_id = None
    imp_file.duplicate_group_id = None
    imp_file.duplicate_of_file_id = None
    imp_file.is_preferred = False
    imp_file.error_message = None
    imp_file.diagnostics = {
        **diagnostics,
        "kind": "file_safety_review",
        "resolution": "skipped",
    }
    imported_series.selected_for_import = False

    await recompute_file_counters(session, job, [imported_series.id])
    await recompute_series_counters(session, job)
    await session.flush()
    return imp_file


def _series_has_match_target(imported_series: ImportedSeries) -> bool:
    """Return true when a review row has a CV/library target to clear."""
    return bool(
        imported_series.cv_id
        or imported_series.user_selected_cv_id
        or imported_series.series_id
        or imported_series.status in (ImportSeriesStatus.MATCHED, ImportSeriesStatus.DUPLICATE)
    )


def _capture_series_match(imported_series: ImportedSeries) -> dict[str, object]:
    """Snapshot the review match before returning the row to manual review."""
    return {
        "status": imported_series.status.value,
        "series_id": imported_series.series_id,
        "cv_id": imported_series.cv_id,
        "cv_title": imported_series.cv_title,
        "cv_year": imported_series.cv_year,
        "cv_publisher": imported_series.cv_publisher,
        "cv_issue_count": imported_series.cv_issue_count,
        "cv_url": imported_series.cv_url,
        "cv_match_score": imported_series.cv_match_score,
        "cv_match_method": imported_series.cv_match_method,
        "user_selected_cv_id": imported_series.user_selected_cv_id,
    }


async def _unmatch_series_target(
    session: AsyncSession,
    job_id: int,
    imported_series_id: int,
    *,
    reason: str,
    rejection_reason: str,
    previous_key: str,
    previous_match: dict[str, object] | None,
    require_duplicate: bool,
    recompute_file_counters: RecomputeFileCounters,
    recompute_series_counters: RecomputeSeriesCounters,
) -> ImportedSeries:
    """Clear a Step 3 series/CV target and return the row to manual review."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if job.status != ImportJobStatus.REVIEW:
        raise ValidationError("Job must be in REVIEW state to unmatch series")

    imported_series = await session.get(ImportedSeries, imported_series_id)
    if imported_series is None or imported_series.import_job_id != job_id:
        raise NotFoundError("ImportedSeries", imported_series_id)

    if require_duplicate:
        if not is_duplicate_series(imported_series):
            raise ValidationError("Only in-library duplicate series can be unmatched")
    elif imported_series.status not in {
        ImportSeriesStatus.MATCHED,
        ImportSeriesStatus.DUPLICATE,
        ImportSeriesStatus.NO_MATCH,
    } or not _series_has_match_target(imported_series):
        raise ValidationError("Only matched or identified review series can be unmatched")

    captured_match = previous_match or _capture_series_match(imported_series)

    imported_series.status = ImportSeriesStatus.NO_MATCH
    imported_series.series_id = None
    imported_series.cv_id = None
    imported_series.cv_title = None
    imported_series.cv_year = None
    imported_series.cv_publisher = None
    imported_series.cv_issue_count = None
    imported_series.cv_url = None
    imported_series.cv_match_score = None
    imported_series.cv_match_method = None
    imported_series.user_selected_cv_id = None
    imported_series.selected_for_import = False
    imported_series.diagnostics = {
        "kind": "series_no_match",
        "reason": reason,
        "normalized_query": NameMatcher.normalize(imported_series.raw_series_name),
        "raw_year": imported_series.raw_year,
        previous_key: captured_match,
    }

    files = list(
        (
            await session.execute(
                sa_select(ImportedFile).where(
                    ImportedFile.import_series_id == imported_series.id,
                )
            )
        )
        .scalars()
        .all()
    )
    for imp_file in files:
        existing_diagnostics = dict(imp_file.diagnostics or {})
        previous_file_match = {
            "status": imp_file.status.value,
            "matched_issue_id": imp_file.matched_issue_id,
            "matched_issue_cv_id": imp_file.matched_issue_cv_id,
            "match_confidence": imp_file.match_confidence,
            "match_method": imp_file.match_method,
            "conflict_group_id": imp_file.conflict_group_id,
            "duplicate_group_id": imp_file.duplicate_group_id,
            "duplicate_of_file_id": imp_file.duplicate_of_file_id,
            "is_preferred": imp_file.is_preferred,
        }
        imp_file.status = ImportedFileStatus.NO_MATCH
        imp_file.include_in_import = False
        imp_file.matched_issue_id = None
        imp_file.matched_issue_cv_id = None
        imp_file.match_confidence = None
        imp_file.match_method = None
        imp_file.conflict_group_id = None
        imp_file.duplicate_group_id = None
        imp_file.duplicate_of_file_id = None
        imp_file.is_preferred = False
        imp_file.diagnostics = {
            **existing_diagnostics,
            "kind": "file_no_match",
            "target_state": "needs_series_match",
            "rejection_reason": rejection_reason,
            "previous_match": previous_file_match,
        }

    await recompute_file_counters(session, job, [imported_series.id])
    await recompute_series_counters(session, job)
    await session.flush()
    return imported_series


async def unmatch_series_match(
    session: AsyncSession,
    job_id: int,
    imported_series_id: int,
    *,
    recompute_file_counters: RecomputeFileCounters,
    recompute_series_counters: RecomputeSeriesCounters,
) -> ImportedSeries:
    """Reject a wrong Step 3 ComicVine/library match and return to series review."""
    return await _unmatch_series_target(
        session,
        job_id,
        imported_series_id,
        reason="series_unmatched_by_user",
        rejection_reason="ComicVine match was rejected by the user.",
        previous_key="previous_match",
        previous_match=None,
        require_duplicate=False,
        recompute_file_counters=recompute_file_counters,
        recompute_series_counters=recompute_series_counters,
    )


async def unmatch_duplicate_series(
    session: AsyncSession,
    job_id: int,
    imported_series_id: int,
    *,
    recompute_file_counters: RecomputeFileCounters,
    recompute_series_counters: RecomputeSeriesCounters,
) -> ImportedSeries:
    """Reject an existing-library duplicate match and return the row to series review."""
    imported_series = await session.get(ImportedSeries, imported_series_id)
    if imported_series is None:
        raise NotFoundError("ImportedSeries", imported_series_id)

    previous_duplicate = {
        "existing_series_id": imported_series.series_id,
        "existing_series_title": (imported_series.diagnostics or {}).get("existing_series_title"),
        "existing_series_year": (imported_series.diagnostics or {}).get("existing_series_year"),
        "duplicate_reason": (imported_series.diagnostics or {}).get("duplicate_reason"),
        "duplicate_match_score": (imported_series.diagnostics or {}).get("duplicate_match_score"),
        "cv_id": imported_series.cv_id,
        "cv_title": imported_series.cv_title,
        "cv_year": imported_series.cv_year,
    }

    return await _unmatch_series_target(
        session,
        job_id,
        imported_series_id,
        reason="duplicate_unmatched_by_user",
        rejection_reason="Existing-library duplicate match was rejected by the user.",
        previous_key="previous_duplicate",
        previous_match=previous_duplicate,
        require_duplicate=True,
        recompute_file_counters=recompute_file_counters,
        recompute_series_counters=recompute_series_counters,
    )


async def bulk_update_series_selection(
    session: AsyncSession,
    job_id: int,
    *,
    include_in_import: bool,
    imported_series_ids: list[int] | None = None,
) -> int:
    """Persist include/exclude state for matched import-review series in scope."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if job.status != ImportJobStatus.REVIEW:
        raise ValidationError("Job must be in REVIEW state to update series selection")

    normalized_ids = sorted(
        {int(series_id) for series_id in imported_series_ids or [] if int(series_id) > 0}
    )
    if not normalized_ids:
        filters = [ImportedSeries.import_job_id == job_id]
        if include_in_import:
            filters.extend(
                [
                    ImportedSeries.status == ImportSeriesStatus.MATCHED,
                    or_(
                        ImportedSeries.files_conflict.is_(None),
                        ImportedSeries.files_conflict == 0,
                    ),
                    ImportedSeries.files_matched > 0,
                ]
            )
        else:
            filters.append(ImportedSeries.selected_for_import.is_(True))

        result = await session.execute(sa_select(ImportedSeries).where(*filters))
        items = list(result.scalars().all())
        for imported_series in items:
            imported_series.selected_for_import = include_in_import
        await session.flush()
        return len(items)

    result = await session.execute(
        sa_select(ImportedSeries).where(
            ImportedSeries.import_job_id == job_id,
            ImportedSeries.id.in_(normalized_ids),
        )
    )
    items = list(result.scalars().all())
    found_ids = {item.id for item in items}
    missing = [series_id for series_id in normalized_ids if series_id not in found_ids]
    if missing:
        raise ValidationError(f"Series IDs {set(missing)} do not belong to this job")

    invalid = {
        item.id: item.status.value for item in items if item.status != ImportSeriesStatus.MATCHED
    }
    if invalid:
        raise ValidationError(
            "Only matched series can be selected for import; "
            f"resolve or exclude these items first: {invalid}"
        )

    unresolved_conflicts = {
        item.id: item.files_conflict for item in items if (item.files_conflict or 0) > 0
    }
    if unresolved_conflicts:
        raise ValidationError(
            "Resolve file conflicts before selecting import series; "
            f"conflicted series: {unresolved_conflicts}"
        )

    empty_matches = {
        item.id for item in items if include_in_import and (item.files_matched or 0) <= 0
    }
    if include_in_import and empty_matches:
        raise ValidationError(
            "Only matched series with importable files can be selected; "
            f"empty matched series: {empty_matches}"
        )

    for imported_series in items:
        imported_series.selected_for_import = include_in_import

    await session.flush()
    return len(items)


async def update_file_selection(
    session: AsyncSession,
    job_id: int,
    file_id: int,
    *,
    include_in_import: bool,
) -> ImportedFile:
    """Persist include/exclude state for an importable duplicate-series file."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if job.status != ImportJobStatus.REVIEW:
        raise ValidationError("Job must be in REVIEW state to update file selection")

    imp_file = await session.get(ImportedFile, file_id)
    if imp_file is None or imp_file.import_job_id != job_id:
        raise NotFoundError("ImportedFile", file_id)

    parent_series = await session.get(ImportedSeries, imp_file.import_series_id)
    if not is_duplicate_series(parent_series):
        raise ValidationError("Only duplicate-series files can be selected individually")
    if not duplicate_merge_is_actionable(parent_series):
        raise ValidationError("This duplicate series has no wanted or missing issues to import.")
    if imp_file.status != ImportedFileStatus.MATCHED:
        raise ValidationError("Only importable matched files can be selected")

    imp_file.include_in_import = include_in_import
    await session.flush()
    return imp_file


async def bulk_update_file_selection(
    session: AsyncSession,
    job_id: int,
    *,
    include_in_import: bool,
    imported_series_id: int | None = None,
) -> int:
    """Persist include/exclude state for all importable duplicate-series files in scope."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if job.status != ImportJobStatus.REVIEW:
        raise ValidationError("Job must be in REVIEW state to update file selection")

    filters = [
        ImportedFile.import_job_id == job_id,
        ImportedSeries.status == ImportSeriesStatus.DUPLICATE,
    ]
    if include_in_import:
        filters.append(
            ImportedFile.status.in_([ImportedFileStatus.MATCHED, ImportedFileStatus.CONFIRMED])
        )
    else:
        filters.append(ImportedFile.include_in_import.is_(True))
    if imported_series_id is not None:
        imported_series = await session.get(ImportedSeries, imported_series_id)
        if imported_series is None or imported_series.import_job_id != job_id:
            raise NotFoundError("ImportedSeries", imported_series_id)
        if include_in_import and not duplicate_merge_is_actionable(imported_series):
            raise ValidationError(
                "This duplicate series has no wanted or missing issues to import."
            )
        filters.append(ImportedFile.import_series_id == imported_series_id)

    result = await session.execute(
        sa_select(ImportedFile, ImportedSeries)
        .join(ImportedSeries, ImportedFile.import_series_id == ImportedSeries.id)
        .where(*filters)
    )
    files = [
        imp_file
        for imp_file, imported_series in result.all()
        if not include_in_import or duplicate_merge_is_actionable(imported_series)
    ]
    for imp_file in files:
        imp_file.include_in_import = include_in_import

    await session.flush()
    return len(files)
