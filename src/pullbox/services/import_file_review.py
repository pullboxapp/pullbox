"""File-level import review and metadata-repair helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.core.source_metadata import SourceMetadataExtractor
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportFileHandlingMode,
    ImportJob,
    ImportJobStatus,
)
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile

if TYPE_CHECKING:
    from pullbox.core.source_metadata import SourceMetadata


class ImportEventLogger(Protocol):
    """Callable contract for writing structured import-job events."""

    def __call__(
        self,
        session: AsyncSession,
        job_id: int,
        level: str,
        event: str,
        message: str | None = None,
        **kwargs: Any,
    ) -> Awaitable[None]: ...


ApplyManualFileMatchFunc = Callable[
    [AsyncSession, ImportedFile, Issue],
    Awaitable[tuple[str, str | None]],
]
BuildComicInfoPayloadFunc = Callable[[AsyncSession, Issue], Awaitable[dict[str, object]]]
DuplicateMergeIsActionableFunc = Callable[[ImportedSeries | None], bool]
DuplicateTargetStateFunc = Callable[[Issue], str | None]
IsDuplicateSeriesFunc = Callable[[ImportedSeries | None], bool]
RecomputeFileCountersFunc = Callable[
    [AsyncSession, ImportJob, list[int]],
    Awaitable[None],
]
RewriteImportFileComicInfoFunc = Callable[
    [ImportedFile, dict[str, object]],
    Awaitable[tuple[Path, str, str]],
]
SourceMetadataExtractorFactory = Callable[[], SourceMetadataExtractor]
SyncImportFileSourceMetadataFunc = Callable[
    [ImportedFile, Path, "SourceMetadata"],
    dict[str, object],
]


class RepairFileMetadataFunc(Protocol):
    """Callable contract for invoking the repair workflow from override."""

    def __call__(
        self,
        session: AsyncSession,
        job_id: int,
        file_id: int,
        *,
        issue_id: int | None = None,
    ) -> Awaitable[ImportedFile]: ...


async def apply_manual_file_match(
    session: AsyncSession,
    imp_file: ImportedFile,
    issue: Issue,
    *,
    method: str,
    is_duplicate_series: IsDuplicateSeriesFunc,
    duplicate_merge_is_actionable: DuplicateMergeIsActionableFunc,
    duplicate_target_state: DuplicateTargetStateFunc,
    confidence: str = "high",
) -> tuple[str, str | None]:
    """Apply a manual issue assignment, respecting duplicate-series merge rules."""
    if imp_file.status == ImportedFileStatus.SAFETY_BLOCKED:
        raise ValidationError("Resolve this file's safety review before assigning an issue.")
    imp_series = await session.get(ImportedSeries, imp_file.import_series_id)
    duplicate_series = is_duplicate_series(imp_series)
    has_library_file = False
    target_state: str | None = None

    if duplicate_series and not duplicate_merge_is_actionable(imp_series):
        raise ValidationError(
            "This duplicate series has no wanted or missing issues; manual reassignment "
            "is disabled in import review."
        )

    if duplicate_series:
        has_library_file = bool(
            await session.scalar(
                sa_select(LibraryFile.id).where(LibraryFile.issue_id == issue.id).limit(1)
            )
        )
        target_state = "already_owned" if has_library_file else duplicate_target_state(issue)

    imp_file.matched_issue_id = issue.id
    imp_file.matched_issue_cv_id = issue.comicvine_id
    imp_file.match_method = method
    imp_file.match_confidence = confidence
    imp_file.conflict_group_id = None
    imp_file.duplicate_group_id = None
    imp_file.duplicate_of_file_id = None
    imp_file.is_preferred = False

    if duplicate_series and has_library_file:
        imp_file.status = ImportedFileStatus.ALREADY_OWNED
        imp_file.include_in_import = False
    else:
        imp_file.status = ImportedFileStatus.MATCHED
        imp_file.include_in_import = False

    imp_file.diagnostics = {
        "kind": "duplicate_series_file" if duplicate_series else "file_match",
        "target_issue_id": issue.id,
        "target_issue_cv_id": issue.comicvine_id,
        "target_issue_number": issue.issue_number,
        "target_issue_title": issue.title,
        "target_state": target_state,
    }

    return imp_file.status.value, target_state


async def override_file_match(
    session: AsyncSession,
    job_id: int,
    file_id: int,
    issue_id: int,
    *,
    repair_source_metadata: bool,
    repair_file_metadata: RepairFileMetadataFunc,
    apply_manual_file_match_func: ApplyManualFileMatchFunc,
    recompute_file_counters: RecomputeFileCountersFunc,
    log_event: ImportEventLogger,
) -> ImportedFile:
    """Manually set a file's issue match."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if job.status != ImportJobStatus.REVIEW:
        raise ValidationError("Job must be in REVIEW state to override file matches")

    imp_file = await session.get(ImportedFile, file_id)
    if imp_file is None or imp_file.import_job_id != job_id:
        raise NotFoundError("ImportedFile", file_id)

    if imp_file.status == ImportedFileStatus.SAFETY_BLOCKED:
        raise ValidationError("Resolve this file's safety review before assigning an issue.")

    issue = await session.get(Issue, issue_id)
    if issue is None:
        raise NotFoundError("Issue", issue_id)

    if repair_source_metadata:
        return await repair_file_metadata(
            session,
            job_id,
            file_id,
            issue_id=issue.id,
        )

    status_value, target_state = await apply_manual_file_match_func(session, imp_file, issue)
    parent_series = await session.get(ImportedSeries, imp_file.import_series_id)
    await recompute_file_counters(
        session,
        job,
        [imp_file.import_series_id],
    )
    await log_event(
        session,
        job_id,
        "INFO",
        "import_file_match_override",
        message=f"Manual file match set for {imp_file.file_name}",
        file_id=imp_file.id,
        imported_series_id=imp_file.import_series_id,
        issue_id=issue.id,
        status=status_value,
        target_state=target_state,
        series=parent_series.raw_series_name if parent_series is not None else None,
    )
    return imp_file


async def repair_file_metadata(
    session: AsyncSession,
    job_id: int,
    file_id: int,
    *,
    build_comicinfo_payload_for_issue: BuildComicInfoPayloadFunc,
    rewrite_import_file_comicinfo: RewriteImportFileComicInfoFunc,
    sync_import_file_source_metadata: SyncImportFileSourceMetadataFunc,
    apply_manual_file_match_func: ApplyManualFileMatchFunc,
    recompute_file_counters: RecomputeFileCountersFunc,
    log_event: ImportEventLogger,
    issue_id: int | None = None,
    source_metadata_extractor_factory: SourceMetadataExtractorFactory = SourceMetadataExtractor,
) -> ImportedFile:
    """Repair embedded ComicInfo metadata for an imported file."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if job.status != ImportJobStatus.REVIEW:
        raise ValidationError("Job must be in REVIEW state to repair file metadata")
    if job.file_handling_mode == ImportFileHandlingMode.IN_PLACE:
        raise ValidationError(
            "In-place import files cannot have embedded metadata rewritten. "
            "Choose a managed copy if Pullbox should repair ComicInfo metadata."
        )

    imp_file = await session.get(ImportedFile, file_id)
    if imp_file is None or imp_file.import_job_id != job_id:
        raise NotFoundError("ImportedFile", file_id)

    if imp_file.status == ImportedFileStatus.SAFETY_BLOCKED:
        raise ValidationError("Resolve this file's safety review before repairing metadata.")

    target_issue_id = issue_id or imp_file.matched_issue_id
    if target_issue_id is None:
        raise ValidationError(
            "Choose the correct issue before repairing embedded ComicInfo metadata."
        )

    issue = await session.get(Issue, target_issue_id)
    if issue is None:
        raise NotFoundError("Issue", target_issue_id)

    comicinfo_payload = await build_comicinfo_payload_for_issue(session, issue)
    (
        repaired_path,
        repair_mode,
        original_source_path,
    ) = await rewrite_import_file_comicinfo(
        imp_file,
        comicinfo_payload,
    )

    refreshed_metadata = source_metadata_extractor_factory().from_archive_path(repaired_path)
    source_metadata_diagnostics = sync_import_file_source_metadata(
        imp_file,
        repaired_path,
        refreshed_metadata,
    )

    status_value, target_state = await apply_manual_file_match_func(session, imp_file, issue)
    diagnostics = dict(imp_file.diagnostics or {})
    diagnostics.update(source_metadata_diagnostics)
    diagnostics.update(
        {
            "metadata_repaired": True,
            "metadata_repair_mode": repair_mode,
            "metadata_repair_original_source_path": original_source_path,
            "metadata_repair_target_path": str(repaired_path),
        }
    )
    imp_file.diagnostics = diagnostics

    await recompute_file_counters(session, job, [imp_file.import_series_id])
    await log_event(
        session,
        job_id,
        "INFO",
        "import_file_metadata_repaired",
        message=f"Repaired embedded ComicInfo metadata for {imp_file.file_name}",
        file_id=imp_file.id,
        imported_series_id=imp_file.import_series_id,
        issue_id=issue.id,
        repair_mode=repair_mode,
        original_source_path=original_source_path,
        repaired_path=str(repaired_path),
        status=status_value,
        target_state=target_state,
    )
    return imp_file
