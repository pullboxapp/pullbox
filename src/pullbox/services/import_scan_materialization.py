"""Materialize scanner results into import review rows."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import insert

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportSeriesStatus,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.core.collection_scanner import DiscoveredSeries
    from pullbox.models.import_job import ImportJob


_SOURCE_LAYOUT_REVIEW_REASON = "selected_layout_no_match"
_SOURCE_LAYOUT_REVIEW_MESSAGE = (
    "This file does not fit the selected source layout. Review its series before importing."
)
_MYLAR_FOLDER_SCOPE_REVIEW_MESSAGE = (
    "This unrecorded file appears to belong to another series in the selected Mylar folder."
)
_VOLUME_LEAF_REVIEW_REASON = "volume_leaf_identity_unconfirmed"
_VOLUME_LEAF_REVIEW_MESSAGE = (
    "The volume folder does not provide enough agreeing evidence for this file. "
    "Confirm its series using the filename or embedded metadata before importing."
)


def _requires_volume_leaf_review(metadata_diagnostics: dict[str, object]) -> bool:
    evidence = metadata_diagnostics.get("volume_leaf")
    return isinstance(evidence, dict) and evidence.get("review_required") is True


def _requires_source_layout_review(metadata_diagnostics: dict[str, object]) -> bool:
    """Return whether a selected no-fallback layout requires explicit review."""
    layout = metadata_diagnostics.get("source_layout")
    return (
        isinstance(layout, dict)
        and layout.get("review_required") is True
        and layout.get("review_reason") == _SOURCE_LAYOUT_REVIEW_REASON
    )


def _requires_mylar_folder_scope_review(metadata_diagnostics: dict[str, object]) -> bool:
    """Return whether an unrecorded file contradicts its owning Mylar series folder."""
    return isinstance(metadata_diagnostics.get("mylar3_folder_scope_conflict"), dict)


async def materialize_discovered_scan_results(
    session: AsyncSession,
    job: ImportJob,
    discovered_list: list[DiscoveredSeries],
    *,
    log_event: Callable[..., Awaitable[None]] | None = None,
) -> list[tuple[DiscoveredSeries, ImportedSeries]]:
    """Persist discovered scanner output as import review series/file rows."""
    series_pairs: list[tuple[DiscoveredSeries, ImportedSeries]] = []
    for discovered in discovered_list:
        mylar_path_incompatible = (
            dict(discovered.diagnostics).get("kind") == "mylar3_path_incompatible"
        )
        layout_review_count = sum(
            _requires_source_layout_review(dict(discovered_file.metadata_diagnostics))
            or _requires_volume_leaf_review(dict(discovered_file.metadata_diagnostics))
            for discovered_file in discovered.files
        )
        all_files_require_layout_review = bool(discovered.files) and layout_review_count == len(
            discovered.files
        )
        series_diagnostics = dict(discovered.diagnostics)
        volume_leaf = series_diagnostics.get("volume_leaf")
        release_review = (
            isinstance(volume_leaf, dict)
            and volume_leaf.get("series_confirmation_required") is True
        )
        if release_review:
            series_diagnostics.update(
                {
                    "kind": "source_layout_review",
                    "reason": "volume_leaf_release_unconfirmed",
                    "rejection_reason": (
                        "The volume folder does not identify a unique series release. "
                        "Confirm the series match before importing."
                    ),
                }
            )
        if layout_review_count:
            series_diagnostics["source_layout_review_files"] = layout_review_count
        if all_files_require_layout_review:
            volume_review = any(
                _requires_volume_leaf_review(dict(file.metadata_diagnostics))
                for file in discovered.files
            )
            series_diagnostics.update(
                {
                    "kind": "source_layout_review",
                    "reason": _VOLUME_LEAF_REVIEW_REASON
                    if volume_review
                    else _SOURCE_LAYOUT_REVIEW_REASON,
                    "rejection_reason": _VOLUME_LEAF_REVIEW_MESSAGE
                    if volume_review
                    else _SOURCE_LAYOUT_REVIEW_MESSAGE,
                }
            )
        item = ImportedSeries(
            import_job_id=job.id,
            raw_series_name=discovered.raw_series_name,
            raw_year=discovered.raw_year,
            raw_publisher=discovered.raw_publisher,
            file_count=discovered.file_count,
            sample_paths=[str(p) for p in discovered.sample_paths],
            source_folder=discovered.source_folder,
            has_files=discovered.has_files,
            status=(
                ImportSeriesStatus.NO_MATCH
                if all_files_require_layout_review or mylar_path_incompatible or release_review
                else ImportSeriesStatus.PENDING
            ),
            diagnostics=series_diagnostics,
        )
        if discovered.mylar3_cv_id:
            item.cv_id = discovered.mylar3_cv_id
            item.cv_match_method = "mylar3_cv_id"
        elif discovered.folder_cv_id:
            item.cv_id = discovered.folder_cv_id
            item.cv_match_method = "folder_cv_id"
        elif discovered.comicinfo_cv_id:
            item.cv_id = discovered.comicinfo_cv_id
            item.cv_match_method = "comicinfo_cv_id"
        session.add(item)
        series_pairs.append((discovered, item))

    job.series_found = len(discovered_list)
    job.scan_completed_at = datetime.now(UTC)
    await session.flush()

    total_files = 0
    file_rows: list[dict[str, object]] = []
    for discovered, series_item in series_pairs:
        series_file_count = 0
        for df in discovered.files:
            metadata_diagnostics = dict(df.metadata_diagnostics)
            safety_block = metadata_diagnostics.pop("file_safety", None)
            source_layout_review = _requires_source_layout_review(metadata_diagnostics)
            volume_leaf_review = _requires_volume_leaf_review(metadata_diagnostics)
            mylar_folder_scope_review = _requires_mylar_folder_scope_review(metadata_diagnostics)
            if isinstance(safety_block, dict):
                file_status = ImportedFileStatus.SAFETY_BLOCKED
            elif source_layout_review or mylar_folder_scope_review or volume_leaf_review:
                file_status = ImportedFileStatus.NO_MATCH
            else:
                file_status = ImportedFileStatus.PENDING
            diagnostics = {
                "source_issue_type": df.issue_type.value,
                "comicvine_series_id": df.comicvine_series_id,
                "series_status": df.series_status,
                "issue_count_hint": df.issue_count_hint,
                "metadata_signals": dict(df.metadata_signals),
                "source_metadata": metadata_diagnostics,
            }
            cross_folder_reconciliation = metadata_diagnostics.get(
                "mylar3_cross_folder_reconciliation"
            )
            if isinstance(cross_folder_reconciliation, dict):
                diagnostics["mylar3_cross_folder_reconciliation"] = dict(
                    cross_folder_reconciliation
                )
            if isinstance(safety_block, dict):
                diagnostics["safety_block"] = safety_block
            elif source_layout_review:
                diagnostics.update(
                    {
                        "kind": "source_layout_review",
                        "reason": "selected_layout_no_match",
                        "rejection_reason": _SOURCE_LAYOUT_REVIEW_MESSAGE,
                    }
                )
            elif volume_leaf_review:
                diagnostics.update(
                    {
                        "kind": "source_scope_review",
                        "reason": _VOLUME_LEAF_REVIEW_REASON,
                        "rejection_reason": _VOLUME_LEAF_REVIEW_MESSAGE,
                        "preserve_series_match": True,
                    }
                )
            elif mylar_folder_scope_review:
                diagnostics.update(
                    {
                        "kind": "source_scope_review",
                        "reason": "mylar3_folder_scope_conflict",
                        "rejection_reason": _MYLAR_FOLDER_SCOPE_REVIEW_MESSAGE,
                        "preserve_series_match": True,
                    }
                )
            error_message = safety_block.get("reason") if isinstance(safety_block, dict) else None
            file_item = dict(
                import_job_id=job.id,
                import_series_id=series_item.id,
                file_path=df.file_path,
                file_name=df.file_name,
                file_size=df.file_size,
                file_format=df.file_format,
                parsed_series=df.parsed_series,
                parsed_issue_number=df.parsed_issue_number,
                parsed_year=df.parsed_year,
                has_comicinfo=df.has_comicinfo,
                comicvine_issue_id=df.comicvine_issue_id,
                issue_number_raw=df.issue_number_raw,
                source_folder_cohort_key=df.source_folder_cohort_key,
                source_ordinal=df.source_ordinal,
                source_signature=dict(df.source_signature),
                status=file_status,
                include_in_import=False,
                error_message=error_message,
                diagnostics=diagnostics,
            )
            file_rows.append(file_item)
            if file_status == ImportedFileStatus.NO_MATCH and log_event is not None:
                await log_event(
                    session,
                    job.id,
                    "DEBUG",
                    "import_file_no_match_detail",
                    message=f"File needs review: {df.file_name}",
                    file_name=df.file_name,
                    parsed_issue_number=df.parsed_issue_number,
                    series=series_item.raw_series_name,
                    reason=diagnostics.get("reason"),
                    diagnostics=diagnostics,
                )
            if len(file_rows) >= 500:
                await session.execute(insert(ImportedFile), file_rows)
                file_rows.clear()
            series_file_count += 1
        series_item.files_total = series_file_count
        total_files += series_file_count
    if file_rows:
        await session.execute(insert(ImportedFile), file_rows)
    if total_files:
        await session.flush()

    return series_pairs
