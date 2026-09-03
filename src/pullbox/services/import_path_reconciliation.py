"""Offline, transactional repair of proven stale Mylar review references."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import aliased

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.core.file_safety import (
    get_archive_size_limit_bytes,
    is_dangerous_file_blocking_enabled,
)
from pullbox.core.filesystem_policy import resolve_preview_source
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobLog,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.services.import_counters import recompute_file_counters
from pullbox.services.import_path_identity import (
    reconciliation_evidence,
    same_trusted_issue,
    unchanged_same_folder_pair,
)
from pullbox.services.import_review_recheck import inspect_review_source
from pullbox.services.import_source_metadata import source_metadata_for_import_file

if TYPE_CHECKING:
    from sqlalchemy import Select
    from sqlalchemy.ext.asyncio import AsyncSession


def _candidate_query(
    job_id: int,
) -> tuple[Select[tuple[ImportedFile, ImportedFile, ImportedSeries]], type[ImportedFile]]:
    # Count all same-folder copies, including blocked ones, before choosing a match.
    folder = func.substr(
        ImportedFile.file_path,
        1,
        func.length(ImportedFile.file_path) - func.length(ImportedFile.file_name),
    )
    groups = select(
        ImportedFile.import_series_id.label("series_id"),
        ImportedFile.comicvine_issue_id.label("issue_id"),
        folder.label("folder"),
        func.count().label("n"),
        func.min(ImportedFile.id).label("id"),
    ).where(ImportedFile.import_job_id == job_id, ImportedFile.comicvine_issue_id.is_not(None))
    group_by = (ImportedFile.import_series_id, ImportedFile.comicvine_issue_id, folder)
    missing = (
        groups.where(
            ImportedFile.diagnostics["safety_block"]["code"].as_string() == "source_missing"
        )
        .group_by(*group_by)
        .cte("missing_references")
    )
    available = (
        groups.where(ImportedFile.file_size > 0).group_by(*group_by).cte("available_sources")
    )
    old, actual = aliased(ImportedFile), aliased(ImportedFile)
    return (
        select(old, actual, ImportedSeries)
        .join(missing, old.id == missing.c.id)
        .join(
            available,
            (missing.c.series_id == available.c.series_id)
            & (missing.c.issue_id == available.c.issue_id)
            & (missing.c.folder == available.c.folder),
        )
        .join(actual, actual.id == available.c.id)
        .join(ImportedSeries, ImportedSeries.id == old.import_series_id)
        .where(
            missing.c.n == 1,
            available.c.n == 1,
            actual.status == ImportedFileStatus.MATCHED,
            old.status == ImportedFileStatus.SAFETY_BLOCKED,
        ),
        old,
    )


async def _protected_series(session: AsyncSession, ids: list[int]) -> set[int]:
    return set(
        await session.scalars(
            select(ImportedFile.import_series_id)
            .where(
                ImportedFile.import_series_id.in_(ids),
                or_(
                    ImportedFile.status.not_in(
                        [
                            ImportedFileStatus.MATCHED,
                            ImportedFileStatus.PENDING,
                            ImportedFileStatus.NO_MATCH,
                            ImportedFileStatus.SAFETY_BLOCKED,
                        ]
                    ),
                    ImportedFile.match_method.startswith("manual"),
                    ImportedFile.include_in_import.is_(True),
                    ImportedFile.diagnostics["safety_exception"]["allowed_once"]
                    .as_boolean()
                    .is_(True),
                ),
            )
            .distinct()
        )
    )


def _retain(report: dict[str, Any], record: ImportedFile, reason: str) -> None:
    counts = report["retained_reasons"]
    counts[reason] = counts.get(reason, 0) + 1
    if len(report["retained_samples"]) < 20:
        report["retained_samples"].append(
            {
                "recorded_file_id": record.id,
                "recorded_path": record.file_path,
                "reason": reason,
            }
        )


async def reconcile_saved_mylar_paths(
    session: AsyncSession,
    job_id: int,
    *,
    source_roots: list[Path],
    apply: bool = False,
    series_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Leave the job in REVIEW; caller commits only after an explicit apply."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if job.status != ImportJobStatus.REVIEW or job.control_request != ImportControlRequest.NONE:
        raise ValidationError("Job must be idle in REVIEW before offline reconciliation")
    if job.source_type != ImportSourceType.MYLAR3:
        raise ValidationError("Stale Mylar reference repair requires a Mylar import")
    if not source_roots:
        raise ValidationError("At least one explicit source root is required")
    roots = [(path.expanduser().absolute(), resolve_preview_source(path)) for path in source_roots]
    if any(not real.is_dir() or real.parent == real for _, real in roots):
        raise ValidationError("Source roots must be specific existing directories")
    dangerous = await is_dangerous_file_blocking_enabled(session)
    limit = await get_archive_size_limit_bytes(session)
    report: dict[str, Any] = {
        "candidates_checked": 0,
        "references_reconciled": 0,
        "candidates_retained": 0,
        "samples": [],
        "retained_reasons": {},
        "retained_samples": [],
    }
    count = (
        select(func.count())
        .select_from(ImportedFile)
        .where(
            ImportedFile.import_job_id == job_id,
            ImportedFile.status == ImportedFileStatus.SAFETY_BLOCKED,
            ImportedFile.diagnostics["safety_block"]["code"].as_string() == "source_missing",
        )
    )
    if series_ids:
        count = count.where(ImportedFile.import_series_id.in_(series_ids))
    report["missing_references"] = await session.scalar(count) or 0
    query, old = _candidate_query(job_id)
    if series_ids:
        query = query.where(ImportedSeries.id.in_(series_ids))
    changed_series: set[int] = set()
    # Stream one grouped query rather than re-aggregate the entire library per page.
    result = await session.stream(query.order_by(old.id).execution_options(yield_per=250))
    try:
        async for rows in result.partitions(250):
            protected = await _protected_series(session, list({s.id for _, _, s in rows}))
            referenced = set(
                await session.scalars(
                    select(ImportedFile.duplicate_of_file_id).where(
                        ImportedFile.duplicate_of_file_id.in_([record.id for record, _, _ in rows])
                    )
                )
            )
            sidecars: dict[str, dict[str, Any]] = {}
            for record, actual, series in rows:
                report["candidates_checked"] += 1
                report["candidates_retained"] += 1
                if (
                    series.id in protected
                    or series.user_selected_cv_id is not None
                    or series.selected_for_import
                    or record.source_signature
                    or actual.diagnostics.get("safety_block")
                    or record.id in referenced
                    or actual.library_file_id is not None
                    or actual.matched_issue_cv_id != record.comicvine_issue_id
                ):
                    _retain(report, record, "review_or_source_protected")
                    continue
                if not await asyncio.to_thread(
                    unchanged_same_folder_pair,
                    Path(record.file_path),
                    Path(actual.file_path),
                    dict(actual.source_signature),
                ):
                    _retain(report, record, "source_check_failed")
                    continue
                base = source_metadata_for_import_file(series, record)
                metadata, content, _signature = await asyncio.to_thread(
                    inspect_review_source,
                    Path(actual.file_path),
                    source_metadata_for_import_file(series, actual),
                    dict(actual.source_signature),
                    roots=roots,
                    block_dangerous=dangerous,
                    max_archive_size=limit,
                    accept_replaced_files=False,
                    sidecars=sidecars,
                )
                if "file_safety" in content:
                    _retain(report, record, "file_safety_review")
                    continue
                if not same_trusted_issue(base, metadata):
                    _retain(report, record, "identity_unconfirmed")
                    continue
                if not await asyncio.to_thread(
                    unchanged_same_folder_pair,
                    Path(record.file_path),
                    Path(actual.file_path),
                    dict(actual.source_signature),
                ):
                    _retain(report, record, "source_changed_during_check")
                    continue
                evidence = {
                    **reconciliation_evidence(
                        record.file_path, actual.file_path, record.comicvine_issue_id
                    ),
                    "recorded_file_id": record.id,
                }
                report["references_reconciled"] += 1
                report["candidates_retained"] -= 1
                if len(report["samples"]) < 20:
                    report["samples"].append(evidence)
                if apply:
                    diagnostics = dict(actual.diagnostics or {})
                    source = dict(diagnostics.get("source_metadata") or {})
                    source["mylar3_path_reconciliation"] = evidence
                    actual.diagnostics = {**diagnostics, "source_metadata": source}
                    await session.delete(record)
                    changed_series.add(series.id)
            if apply:
                await session.flush()
    finally:
        await result.close()
    unmatched = report["missing_references"] - report["candidates_checked"]
    if unmatched:
        report["retained_reasons"]["no_unique_matched_counterpart"] = unmatched
    report["remaining_missing_references"] = (
        report["missing_references"] - report["references_reconciled"]
    )
    if apply and report["references_reconciled"]:
        await recompute_file_counters(session, job)
        for series_id in changed_series:
            series = await session.get(ImportedSeries, series_id)
            if series is not None:
                series.file_count = series.files_total
                series.sample_paths = list(
                    await session.scalars(
                        select(ImportedFile.file_path)
                        .where(ImportedFile.import_series_id == series.id)
                        .order_by(ImportedFile.id)
                        .limit(5)
                    )
                )
        session.add(
            ImportJobLog(
                import_job_id=job.id,
                level="INFO",
                event="import_mylar_paths_reconciled",
                message=(
                    f"Reconciled {report['references_reconciled']} stale Mylar references "
                    "with verified files; source files and user decisions unchanged."
                ),
                data=report,
            )
        )
        await session.flush()
    return report
