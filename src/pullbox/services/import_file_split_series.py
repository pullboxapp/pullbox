"""Split-series rebucketing helpers for import file matching."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select as sa_select

from pullbox.core.exceptions import ImportProviderDegradedError
from pullbox.core.source_metadata import MetadataSignal
from pullbox.core.type_semantics import TypeFamily, issue_type_family
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportSeriesStatus,
)
from pullbox.models.series import Series

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.core.source_metadata import SourceMetadata
    from pullbox.providers.base import MetadataProvider

    SourceMetadataForImportFileFunc = Callable[[ImportedSeries, ImportedFile], SourceMetadata]
    LogEventFunc = Callable[..., Awaitable[None]]


async def split_explicit_issue_series_mismatches(
    session: AsyncSession,
    job: ImportJob,
    imp_series: ImportedSeries,
    files: list[ImportedFile],
    *,
    metadata_provider: MetadataProvider | None,
    source_metadata_for_import_file: SourceMetadataForImportFileFunc,
    target_series: Series | None,
    log_event: LogEventFunc,
) -> tuple[list[ImportedFile], list[int]]:
    """Move collection files into their own matched row when issue metadata proves it."""
    if metadata_provider is None:
        return files, []

    current_target_cv_id = (
        int(target_series.comicvine_id)
        if target_series is not None and target_series.comicvine_id is not None
        else (int(imp_series.cv_id) if imp_series.cv_id is not None else None)
    )
    if current_target_cv_id is None:
        return files, []

    split_series_by_cv_id: dict[int, ImportedSeries] = {}
    created_split_series_ids: list[int] = []
    remaining_files: list[ImportedFile] = []
    moved_file_ids: list[int] = []
    moved_file_paths: list[str] = []

    for imp_file in files:
        metadata = source_metadata_for_import_file(imp_series, imp_file)
        issue_signal = metadata.signals.get("comicvine_issue_id")
        if (
            metadata.comicvine_issue_id is None
            or issue_signal not in {MetadataSignal.COMICINFO, MetadataSignal.SIDECAR}
            or issue_type_family(metadata.issue_type) != TypeFamily.COLLECTION
        ):
            remaining_files.append(imp_file)
            continue

        series_signal = metadata.signals.get("comicvine_series_id")
        if (
            metadata.comicvine_series_id == current_target_cv_id
            and series_signal in {MetadataSignal.COMICINFO, MetadataSignal.SIDECAR}
            and not metadata.diagnostics.get("identity_conflicts")
        ):
            remaining_files.append(imp_file)
            continue

        try:
            issue_meta = await metadata_provider.get_issue(str(metadata.comicvine_issue_id))
        except Exception as exc:
            raise ImportProviderDegradedError(
                provider="ComicVine",
                query=imp_file.file_name,
                year=metadata.year,
                attempts=1,
                last_error=str(exc),
            ) from exc

        resolved_series_cv_id = int(issue_meta.series_provider_id or 0)
        if not resolved_series_cv_id or resolved_series_cv_id == current_target_cv_id:
            remaining_files.append(imp_file)
            continue

        split_series = split_series_by_cv_id.get(resolved_series_cv_id)
        if split_series is None:
            split_series = await _load_or_create_split_series(
                session,
                job,
                parent_series=imp_series,
                file_metadata=metadata,
                split_series_cv_id=resolved_series_cv_id,
                metadata_provider=metadata_provider,
                trigger_issue_cv_id=int(metadata.comicvine_issue_id),
                source_target_cv_id=current_target_cv_id,
                target_series=target_series,
            )
            split_series_by_cv_id[resolved_series_cv_id] = split_series
            if split_series.id not in created_split_series_ids:
                created_split_series_ids.append(split_series.id)

        move_file_to_split_series(
            imp_file,
            split_series=split_series,
            parent_series=imp_series,
            trigger_issue_cv_id=int(metadata.comicvine_issue_id),
            resolved_series_cv_id=resolved_series_cv_id,
        )
        moved_file_ids.append(imp_file.id)
        moved_file_paths.append(imp_file.file_path)
        await log_event(
            session,
            job.id,
            "INFO",
            "import_file_rebucketed_to_split_series",
            message=(
                f"Rebucketed {imp_file.file_name} into its own import row for "
                f"{split_series.cv_title or split_series.raw_series_name}"
            ),
            file_name=imp_file.file_name,
            source_import_series_id=imp_series.id,
            source_import_series_name=imp_series.raw_series_name,
            target_import_series_id=split_series.id,
            target_series_cv_id=resolved_series_cv_id,
            trigger_issue_cv_id=int(metadata.comicvine_issue_id),
            signal=issue_signal.value,
        )

    if moved_file_ids:
        imp_series.sample_paths = [
            path for path in list(imp_series.sample_paths or []) if path not in moved_file_paths
        ]
        pre_move_file_count = int(imp_series.file_count or 0)
        diagnostics = dict(imp_series.diagnostics or {})
        diagnostics.setdefault(
            "pre_split_file_count",
            max(pre_move_file_count, int(imp_series.files_total or 0)),
        )
        imp_series.file_count = max(pre_move_file_count - len(moved_file_ids), 0)
        accumulated_file_ids = list(diagnostics.get("moved_file_ids") or [])
        accumulated_split_ids = list(diagnostics.get("split_series_ids") or [])
        diagnostics["moved_file_ids"] = list(
            dict.fromkeys([*accumulated_file_ids, *moved_file_ids])
        )
        diagnostics["split_series_ids"] = list(
            dict.fromkeys([*accumulated_split_ids, *created_split_series_ids])
        )
        if int(imp_series.file_count or 0) == 0:
            imp_series.status = ImportSeriesStatus.SKIPPED
            imp_series.selected_for_import = False
            diagnostics.update(
                {
                    "kind": "rebucketed_series",
                    "reason": "all_files_moved_to_split_series",
                }
            )
        imp_series.diagnostics = diagnostics

    return remaining_files, created_split_series_ids


async def _load_or_create_split_series(
    session: AsyncSession,
    job: ImportJob,
    *,
    parent_series: ImportedSeries,
    file_metadata: SourceMetadata,
    split_series_cv_id: int,
    metadata_provider: MetadataProvider,
    trigger_issue_cv_id: int,
    source_target_cv_id: int,
    target_series: Series | None,
) -> ImportedSeries:
    existing_split_result = await session.execute(
        sa_select(ImportedSeries)
        .where(
            ImportedSeries.import_job_id == job.id,
            ImportedSeries.cv_id == split_series_cv_id,
            ImportedSeries.id != parent_series.id,
            ImportedSeries.status.in_(
                [
                    ImportSeriesStatus.PENDING,
                    ImportSeriesStatus.MATCHED,
                    ImportSeriesStatus.DUPLICATE,
                ]
            ),
        )
        .order_by(ImportedSeries.id.asc())
    )
    existing_split = existing_split_result.scalars().first()
    if existing_split is not None:
        return existing_split

    try:
        split_series_meta = await metadata_provider.get_series(str(split_series_cv_id))
    except Exception as exc:
        raise ImportProviderDegradedError(
            provider="ComicVine",
            query=parent_series.raw_series_name,
            year=file_metadata.year,
            attempts=1,
            last_error=str(exc),
        ) from exc

    existing_library_series_result = await session.execute(
        sa_select(Series).where(Series.comicvine_id == split_series_cv_id)
    )
    existing_library_series = existing_library_series_result.scalars().first()

    diagnostics: dict[str, Any] = {
        "source_issue_type": file_metadata.issue_type.value,
        "split_reason": "explicit_issue_series_mismatch",
        "source_import_series_id": parent_series.id,
        "source_import_series_name": parent_series.raw_series_name,
        "source_target_cv_id": source_target_cv_id,
        "source_target_cv_title": (
            target_series.title
            if target_series is not None
            else (parent_series.cv_title or parent_series.raw_series_name)
        ),
        "trigger_issue_cv_id": trigger_issue_cv_id,
        "trigger_series_cv_id": split_series_cv_id,
        "trigger_series_title": split_series_meta.title,
    }

    split_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name=file_metadata.series_name or parent_series.raw_series_name,
        raw_year=file_metadata.year or split_series_meta.year_start or parent_series.raw_year,
        raw_publisher=parent_series.raw_publisher or split_series_meta.publisher,
        file_count=0,
        sample_paths=[],
        source_folder=parent_series.source_folder,
        has_files=True,
        cv_id=int(split_series_meta.provider_id),
        cv_title=split_series_meta.title,
        cv_year=split_series_meta.year_start,
        cv_publisher=split_series_meta.publisher,
        cv_issue_count=split_series_meta.issue_count,
        cv_url=split_series_meta.comicvine_url,
        cv_match_score=1.0,
        cv_match_method="explicit_issue_series_split",
        status=ImportSeriesStatus.MATCHED,
        selected_for_import=False,
        diagnostics=diagnostics,
    )
    if existing_library_series is not None:
        split_series.status = ImportSeriesStatus.DUPLICATE
        split_series.series_id = existing_library_series.id
        split_series.diagnostics = {
            **diagnostics,
            "kind": "duplicate_series",
            "duplicate_reason": "cv_id",
            "existing_series_id": existing_library_series.id,
            "existing_series_title": existing_library_series.title,
            "existing_series_year": existing_library_series.year_start,
        }

    session.add(split_series)
    await session.flush()
    return split_series


def move_file_to_split_series(
    imp_file: ImportedFile,
    *,
    split_series: ImportedSeries,
    parent_series: ImportedSeries,
    trigger_issue_cv_id: int,
    resolved_series_cv_id: int,
) -> None:
    imp_file.import_series_id = split_series.id
    imp_file.status = ImportedFileStatus.PENDING
    imp_file.include_in_import = False
    imp_file.matched_issue_id = None
    imp_file.matched_issue_cv_id = None
    imp_file.match_confidence = None
    imp_file.match_method = None
    imp_file.conflict_group_id = None
    imp_file.duplicate_group_id = None
    imp_file.duplicate_of_file_id = None
    imp_file.is_preferred = False
    imp_file.error_message = None
    imp_file.content_hash = None

    diagnostics = dict(imp_file.diagnostics or {})
    diagnostics["split_series"] = {
        "reason": "explicit_issue_series_mismatch",
        "source_import_series_id": parent_series.id,
        "source_import_series_name": parent_series.raw_series_name,
        "target_import_series_id": split_series.id,
        "target_series_cv_id": resolved_series_cv_id,
        "trigger_issue_cv_id": trigger_issue_cv_id,
    }
    imp_file.diagnostics = diagnostics

    sample_paths = list(split_series.sample_paths or [])
    if imp_file.file_path not in sample_paths:
        sample_paths.append(imp_file.file_path)
    split_series.sample_paths = sample_paths
    split_series.file_count = int(split_series.file_count or 0) + 1
