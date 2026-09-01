"""ImportService adapter for confirmed series file processing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pullbox.services.import_file_execution import process_import_series_files
from pullbox.utilities.settings import move_file_to_utility_trash

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from pullbox.core.file_ops import LibraryFileRegistrationOutcome
    from pullbox.core.library_permissions import LibraryPermissionPolicy
    from pullbox.core.library_policy import LibraryIngestPolicy
    from pullbox.models.import_job import ImportedSeries, ImportJob, ImportJobAction
    from pullbox.models.issue import Issue
    from pullbox.models.library import LibraryFile, LibraryFileStorageMode, MatchConfidence
    from pullbox.services.import_file_execution_protocols import ReportFileProgressFunc
    from pullbox.services.import_file_preparation import PreparedImportFile

    CoreSeriesFileProcessor = Callable[..., Awaitable[tuple[int, int]]]
    LoadMediaSettingsFunc = Callable[[AsyncSession, ImportJob], Awaitable[dict[str, str]]]
    LoadTrashDirFunc = Callable[[AsyncSession, ImportJob], Awaitable[Path]]
    LoadIngestPolicyFunc = Callable[[AsyncSession, ImportJob], Awaitable[LibraryIngestPolicy]]
    LoadPermissionPolicyFunc = Callable[
        [AsyncSession, ImportJob],
        Awaitable[LibraryPermissionPolicy],
    ]
    RaiseIfCancelledFunc = Callable[[AsyncSession, int], Awaitable[None]]
    PrepareFileFunc = Callable[..., Awaitable[PreparedImportFile]]
    BuildCachedComicInfoPayloadFunc = Callable[..., Awaitable[dict[str, Any]]]
    ApplyComicInfoFunc = Callable[[Path, dict[str, Any]], None]
    CleanupPreparedFileFunc = Callable[[PreparedImportFile], None]
    RecordActionFunc = Callable[..., Awaitable[ImportJobAction]]
    LogEventFunc = Callable[..., Awaitable[None]]
    RegisterImportLibraryFileFunc = Callable[
        ...,
        Awaitable[LibraryFile | LibraryFileRegistrationOutcome],
    ]
    MoveToTrashFunc = Callable[..., Any]


async def process_series_files_for_import(
    session: AsyncSession,
    job: ImportJob,
    item: ImportedSeries,
    *,
    duplicate_mode: bool = False,
    series_id_override: int | None = None,
    report_file_progress: ReportFileProgressFunc | None = None,
    defer_comicinfo_enrichment: bool = True,
    file_worker_count: int = 1,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    load_media_settings: LoadMediaSettingsFunc,
    load_trash_dir: LoadTrashDirFunc,
    load_ingest_policy: LoadIngestPolicyFunc,
    load_permission_policy: LoadPermissionPolicyFunc,
    raise_if_cancelled: RaiseIfCancelledFunc,
    prepare_file: PrepareFileFunc,
    build_cached_comicinfo_payload: BuildCachedComicInfoPayloadFunc,
    apply_comicinfo: ApplyComicInfoFunc,
    cleanup_prepared_file: CleanupPreparedFileFunc,
    record_action: RecordActionFunc,
    log_event: LogEventFunc,
    register_import_library_file: RegisterImportLibraryFileFunc,
    move_to_trash: MoveToTrashFunc = move_file_to_utility_trash,
    core_processor: CoreSeriesFileProcessor = process_import_series_files,
) -> tuple[int, int]:
    """Wire ImportService job-scoped callbacks into the core file processor."""

    async def build_comicinfo_payload(
        session: AsyncSession,
        issue: Issue,
        *,
        source_path: Path | None = None,
        defer_issue_enrichment: bool = False,
    ) -> dict[str, Any]:
        return await build_cached_comicinfo_payload(
            session,
            job,
            issue,
            source_path=source_path,
            defer_issue_enrichment=defer_issue_enrichment,
        )

    async def register_import_file(
        session: AsyncSession,
        source_path: Path,
        issue: Issue,
        confidence: MatchConfidence,
        *,
        move_to_library: bool,
        storage_mode: LibraryFileStorageMode,
        expected_source_signature: dict[str, object] | None,
        library_root_id: int | None,
        transfer_method: str | None,
        normalize_to_cbz: bool | None = None,
        update_embedded_comicinfo_from_match: bool | None = None,
        comicinfo_payload: dict[str, Any] | None = None,
        loaded_issue: Issue | None = None,
        ingest_policy: LibraryIngestPolicy | None = None,
        permission_policy: LibraryPermissionPolicy | None = None,
        transfer_progress_callback: Callable[[str, int, int, str], Awaitable[None] | None]
        | None = None,
        comicinfo_progress_callback: Callable[[str, int, int, str], Awaitable[None] | None]
        | None = None,
        recovery_imported_file_id: int | None = None,
        recovery_original_source_path: Path | None = None,
        source_scan_root: Path | None = None,
        strict_import_target: bool = False,
    ) -> LibraryFile | LibraryFileRegistrationOutcome:
        return await register_import_library_file(
            session,
            job,
            source_path,
            issue,
            confidence,
            move_to_library=move_to_library,
            storage_mode=storage_mode,
            expected_source_signature=expected_source_signature,
            library_root_id=library_root_id,
            transfer_method=transfer_method,
            normalize_to_cbz=normalize_to_cbz,
            update_embedded_comicinfo_from_match=update_embedded_comicinfo_from_match,
            comicinfo_payload=comicinfo_payload,
            loaded_issue=loaded_issue,
            ingest_policy=ingest_policy,
            permission_policy=permission_policy,
            transfer_progress_callback=transfer_progress_callback,
            comicinfo_progress_callback=comicinfo_progress_callback,
            recovery_imported_file_id=recovery_imported_file_id,
            recovery_original_source_path=recovery_original_source_path,
            source_scan_root=source_scan_root,
            strict_import_target=strict_import_target,
        )

    return await core_processor(
        session,
        job,
        item,
        duplicate_mode=duplicate_mode,
        series_id_override=series_id_override,
        load_media_settings=load_media_settings,
        load_trash_dir=load_trash_dir,
        load_ingest_policy=load_ingest_policy,
        load_permission_policy=load_permission_policy,
        raise_if_cancelled=raise_if_cancelled,
        prepare_file=prepare_file,
        build_comicinfo_payload=build_comicinfo_payload,
        apply_comicinfo=apply_comicinfo,
        cleanup_prepared_file=cleanup_prepared_file,
        record_action=record_action,
        log_event=log_event,
        register_file=register_import_file,
        move_to_trash=move_to_trash,
        report_file_progress=report_file_progress,
        defer_comicinfo_enrichment=defer_comicinfo_enrichment,
        revalidate_managed_sources=True,
        file_worker_count=file_worker_count,
        session_factory=session_factory,
    )
