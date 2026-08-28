"""File placement helpers for confirmed import review files."""

from __future__ import annotations

import asyncio
import shutil
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import structlog
from sqlalchemy import func as sa_func
from sqlalchemy import select as sa_select
from sqlalchemy.orm import joinedload

from pullbox.core.exceptions import JobCancelledError, JobPausedError
from pullbox.core.file_ops import LibraryFileRegistrationOutcome
from pullbox.core.file_safety import classify_resource_safety_exception
from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportJobAction
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import LibraryFile, MatchConfidence
from pullbox.models.series import Series
from pullbox.services.import_file_match_targets import (
    PROVIDER_MISSING_ISSUE_PLACEHOLDER_KIND,
    PROVIDER_ZERO_ISSUE_PLACEHOLDER_KIND,
)
from pullbox.services.import_file_resolution import (
    load_importable_files,
    load_issue_lookup_for_series,
)
from pullbox.services.import_folder_adoption import apply_import_series_folder_adoption
from pullbox.utilities.settings import restore_file_from_utility_trash

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from pullbox.models.import_job import ImportedSeries, ImportJob
    from pullbox.services.import_file_execution_protocols import (
        ApplyComicInfoFunc,
        BuildComicInfoPayloadFunc,
        CleanupPreparedFileFunc,
        LoadIngestPolicyFunc,
        LoadMediaSettingsFunc,
        LoadPermissionPolicyFunc,
        LoadTrashDirFunc,
        LogEventFunc,
        MoveToTrashFunc,
        PrepareImportFileFunc,
        RaiseIfCancelledFunc,
        RecordActionFunc,
        RegisterLibraryFileFunc,
        ReportFileProgressFunc,
    )
    from pullbox.services.import_file_preparation import PreparedImportFile

logger = structlog.get_logger(__name__)
_COMICINFO_METADATA_PROGRESS_HEARTBEAT_SECONDS = 5.0
_COMICINFO_METADATA_STAGE = "comicinfo_metadata"
_COMICINFO_ENRICHMENT_DIAGNOSTIC_KEY = "comicinfo_enrichment"

_MATCH_CONFIDENCE_MAP: dict[str, MatchConfidence] = {
    "high": MatchConfidence.HIGH,
    "medium": MatchConfidence.MEDIUM,
    "low": MatchConfidence.LOW,
}


@dataclass(frozen=True, slots=True)
class _PlaceholderIssueTarget:
    issue_number: float
    issue_type: IssueType
    issue_title: str | None
    metadata_source: str


def _cleanup_failed_library_artifact(
    *,
    destination_path: Path | None,
    original_source: Path | None,
    original_trash_path: Path | None,
    transfer_method: str | None,
    created_series_folder: bool,
    created_series_folder_path: Path | None,
) -> None:
    """Best-effort cleanup when import fails after the library artifact was placed."""
    if (
        original_trash_path is not None
        and original_source is not None
        and original_trash_path.exists()
    ):
        restore_file_from_utility_trash(original_trash_path, original_source)
        if destination_path is not None and destination_path.exists():
            destination_path.unlink(missing_ok=True)
    elif (
        transfer_method in {"move", "leave_in_place"}
        and destination_path is not None
        and destination_path.exists()
        and original_source is not None
        and destination_path != original_source
    ):
        original_source.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(destination_path), str(original_source))
    elif destination_path is not None and destination_path.exists():
        destination_path.unlink(missing_ok=True)

    if (
        created_series_folder
        and created_series_folder_path is not None
        and created_series_folder_path.exists()
    ):
        try:
            next(created_series_folder_path.iterdir())
        except StopIteration:
            created_series_folder_path.rmdir()
        except OSError:
            pass


async def _load_issue_for_processing(
    session: AsyncSession,
    issue_id: int,
) -> Issue | None:
    result = await session.execute(
        sa_select(Issue)
        .options(joinedload(Issue.series).joinedload(Series.publisher))
        .where(Issue.id == issue_id)
    )
    return result.scalars().first()


async def _load_imported_file_for_processing(
    session: AsyncSession,
    imported_file_id: int,
) -> ImportedFile | None:
    return await session.get(ImportedFile, imported_file_id)


def _resolve_import_file_issue_id(
    imp_file: ImportedFile,
    *,
    cv_id_to_issue_id: dict[int, int],
    number_to_issue_id: dict[float, int],
) -> int | None:
    if imp_file.matched_issue_id is not None:
        return imp_file.matched_issue_id

    if imp_file.matched_issue_cv_id:
        resolved_issue_id = cv_id_to_issue_id.get(imp_file.matched_issue_cv_id)
        if resolved_issue_id is not None:
            return resolved_issue_id

    if imp_file.comicvine_issue_id:
        resolved_issue_id = cv_id_to_issue_id.get(imp_file.comicvine_issue_id)
        if resolved_issue_id is not None:
            return resolved_issue_id

    placeholder_target = _placeholder_issue_target_from_diagnostics(imp_file)
    if placeholder_target is not None:
        return number_to_issue_id.get(placeholder_target.issue_number)

    if imp_file.parsed_issue_number is not None:
        return number_to_issue_id.get(imp_file.parsed_issue_number)

    return None


def _prepared_file_name_collision_key(job: ImportJob, imp_file: ImportedFile) -> str:
    """Return the basename workers may hand to the library target resolver."""
    file_name = str(imp_file.file_name or Path(str(imp_file.file_path)).name)
    path = Path(file_name)
    if (
        job.move_to_library
        and (job.convert_to_preferred_format or job.update_embedded_comicinfo_from_match)
        and path.suffix.lower() != ".cbz"
    ):
        path = path.with_suffix(".cbz")
    return path.name.casefold()


async def _requires_serial_file_processing(
    session: AsyncSession,
    job: ImportJob,
    *,
    resolved_series_id: int,
    importable_files: list[ImportedFile],
) -> bool:
    cv_id_to_issue, number_to_issue = await load_issue_lookup_for_series(
        session,
        resolved_series_id,
    )
    cv_id_to_issue_id = {
        comicvine_id: issue.id
        for comicvine_id, issue in cv_id_to_issue.items()
        if issue.id is not None
    }
    number_to_issue_id = {
        issue_number: issue.id
        for issue_number, issue in number_to_issue.items()
        if issue.id is not None
    }
    seen_issue_ids: set[int] = set()
    seen_file_names: set[str] = set()
    for imp_file in importable_files:
        file_name_key = _prepared_file_name_collision_key(job, imp_file)
        if file_name_key in seen_file_names:
            return True
        seen_file_names.add(file_name_key)

        resolved_issue_id = _resolve_import_file_issue_id(
            imp_file,
            cv_id_to_issue_id=cv_id_to_issue_id,
            number_to_issue_id=number_to_issue_id,
        )
        if resolved_issue_id is None:
            continue
        if resolved_issue_id in seen_issue_ids:
            return True
        seen_issue_ids.add(resolved_issue_id)
    return False


def _placeholder_issue_target_from_diagnostics(
    imp_file: ImportedFile,
) -> _PlaceholderIssueTarget | None:
    diagnostics = imp_file.diagnostics if isinstance(imp_file.diagnostics, dict) else {}
    kind = diagnostics.get("kind")
    if kind not in {
        PROVIDER_ZERO_ISSUE_PLACEHOLDER_KIND,
        PROVIDER_MISSING_ISSUE_PLACEHOLDER_KIND,
    }:
        return None
    try:
        target_issue_number = diagnostics.get("target_issue_number")
        if not isinstance(target_issue_number, int | float | str):
            return None
        issue_number = float(target_issue_number)
        issue_type = IssueType(str(diagnostics.get("target_issue_type")))
    except (TypeError, ValueError):
        return None
    issue_title = diagnostics.get("target_issue_title")
    return _PlaceholderIssueTarget(
        issue_number=issue_number,
        issue_type=issue_type,
        issue_title=str(issue_title) if issue_title else None,
        metadata_source=(
            "provisional_import"
            if kind == PROVIDER_MISSING_ISSUE_PLACEHOLDER_KIND
            else "import_placeholder"
        ),
    )


async def _ensure_placeholder_issue_targets(
    session: AsyncSession,
    *,
    series_id: int,
    importable_files: list[ImportedFile],
) -> bool:
    placeholder_targets = [
        target
        for imp_file in importable_files
        if (target := _placeholder_issue_target_from_diagnostics(imp_file)) is not None
    ]
    if not placeholder_targets:
        return False

    issues_result = await session.execute(sa_select(Issue).where(Issue.series_id == series_id))
    existing_by_number = {issue.issue_number: issue for issue in issues_result.scalars().all()}
    created_count = 0
    max_target_issue_number = 0
    for target in placeholder_targets:
        existing = existing_by_number.get(target.issue_number)
        if existing is not None:
            if existing.issue_type == IssueType.ISSUE:
                existing.issue_type = target.issue_type
            if target.issue_title and not existing.title:
                existing.title = target.issue_title
            continue
        if float(target.issue_number).is_integer() and target.issue_number > 0:
            max_target_issue_number = max(max_target_issue_number, int(target.issue_number))
        issue = Issue(
            series_id=series_id,
            comicvine_id=None,
            issue_number=target.issue_number,
            title=target.issue_title,
            issue_type=target.issue_type,
            status=IssueStatus.SKIPPED,
            metadata_source=target.metadata_source,
        )
        session.add(issue)
        existing_by_number[target.issue_number] = issue
        created_count += 1

    if created_count:
        series = await session.get(Series, series_id)
        if series is not None:
            series.issue_count = max(
                int(series.issue_count or 0),
                len(existing_by_number),
                max_target_issue_number,
            )
    await session.flush()
    return True


def _registration_outcome(
    result: LibraryFile | LibraryFileRegistrationOutcome,
) -> tuple[LibraryFile, LibraryFileRegistrationOutcome | None]:
    if isinstance(result, LibraryFileRegistrationOutcome):
        return result.library_file, result
    return result, None


def _permission_restore_payload(
    registration: LibraryFileRegistrationOutcome | None,
    *,
    original_source: Path,
    transfer_method: str,
) -> list[dict[str, object]]:
    if registration is None or transfer_method not in {"hardlink", "symlink"}:
        return []

    restore_entries: list[dict[str, str | int]] = []
    for result in registration.permission_results:
        if (
            result.action.value != "applied"
            or result.previous_mode is None
            or result.target_kind.value not in {"file", "hardlink", "symlink"}
        ):
            continue
        restore_entries.append(
            {
                "path": str(original_source),
                "mode": int(result.previous_mode),
            }
        )

    deduped: list[dict[str, object]] = []
    seen: set[tuple[str, int]] = set()
    for entry in restore_entries:
        key = (str(entry["path"]), int(cast("int", entry["mode"])))
        if key in seen:
            continue
        seen.add(key)
        deduped.append({"path": entry["path"], "mode": entry["mode"]})
    return deduped


async def _build_comicinfo_payload_with_progress(
    session: AsyncSession,
    issue: Issue,
    *,
    source_path: Path,
    build_comicinfo_payload: BuildComicInfoPayloadFunc,
    report_file_progress: ReportFileProgressFunc | None,
    imp_file: ImportedFile,
    file_index: int,
    total_files: int,
    defer_issue_enrichment: bool = False,
) -> dict[str, Any]:
    """Build ComicInfo data while keeping Step 4 visibly alive."""
    if report_file_progress is None:
        if defer_issue_enrichment:
            return await build_comicinfo_payload(
                session,
                issue,
                source_path=source_path,
                defer_issue_enrichment=True,
            )
        return await build_comicinfo_payload(
            session,
            issue,
            source_path=source_path,
        )

    async def _emit_metadata_progress(current: int) -> None:
        await report_file_progress(
            imp_file=imp_file,
            file_index=file_index,
            total_files=total_files,
            stage=_COMICINFO_METADATA_STAGE,
            current=current,
            total=1,
            unit="steps",
            live_only=True,
        )

    await _emit_metadata_progress(0)
    if defer_issue_enrichment:
        payload_task = asyncio.create_task(
            build_comicinfo_payload(
                session,
                issue,
                source_path=source_path,
                defer_issue_enrichment=True,
            )
        )
    else:
        payload_task = asyncio.create_task(
            build_comicinfo_payload(
                session,
                issue,
                source_path=source_path,
            )
        )
    try:
        while True:
            try:
                payload = await asyncio.wait_for(
                    asyncio.shield(payload_task),
                    timeout=_COMICINFO_METADATA_PROGRESS_HEARTBEAT_SECONDS,
                )
            except TimeoutError:
                if payload_task.done():
                    payload = payload_task.result()
                    await _emit_metadata_progress(1)
                    return payload
                await _emit_metadata_progress(0)
                continue
            await _emit_metadata_progress(1)
            return payload
    except BaseException:
        if not payload_task.done():
            payload_task.cancel()
            with suppress(BaseException):
                await payload_task
        raise


async def process_import_series_files(
    session: AsyncSession,
    job: ImportJob,
    item: ImportedSeries,
    *,
    duplicate_mode: bool = False,
    series_id_override: int | None = None,
    load_media_settings: LoadMediaSettingsFunc,
    load_trash_dir: LoadTrashDirFunc,
    load_ingest_policy: LoadIngestPolicyFunc,
    load_permission_policy: LoadPermissionPolicyFunc,
    raise_if_cancelled: RaiseIfCancelledFunc,
    prepare_file: PrepareImportFileFunc,
    build_comicinfo_payload: BuildComicInfoPayloadFunc,
    apply_comicinfo: ApplyComicInfoFunc,
    cleanup_prepared_file: CleanupPreparedFileFunc,
    record_action: RecordActionFunc,
    log_event: LogEventFunc,
    register_file: RegisterLibraryFileFunc,
    move_to_trash: MoveToTrashFunc,
    report_file_progress: ReportFileProgressFunc | None = None,
    defer_comicinfo_enrichment: bool = False,
    file_worker_count: int = 1,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    _file_ids_override: list[int] | None = None,
    _file_index_by_id: dict[int, int] | None = None,
    _total_file_count: int | None = None,
    _update_item_counters: bool = True,
    _setup_placeholder_targets: bool = True,
) -> tuple[int, int]:
    """Register selected import files into the library and update row counters."""
    if _file_ids_override is None:
        importable_files = await load_importable_files(
            session,
            item,
            duplicate_mode=duplicate_mode,
        )
    else:
        importable_files = [
            imp_file
            for imp_file_id in _file_ids_override
            if (imp_file := await session.get(ImportedFile, imp_file_id)) is not None
        ]
    if not importable_files:
        return 0, 0
    importable_file_ids = [imp_file.id for imp_file in importable_files]

    resolved_series_id = series_id_override or item.series_id
    if resolved_series_id is None:
        raise ValueError("Resolved series id is required before importing files")
    item_id = item.id
    job_id = job.id
    move_to_library = bool(job.move_to_library)
    transfer_method = job.effective_transfer_method or job.transfer_method
    target_library_root_id = job.target_library_root_id
    update_embedded_comicinfo_from_match = bool(job.update_embedded_comicinfo_from_match)
    ingest_policy = await load_ingest_policy(session, job)

    if _file_ids_override is None:
        await apply_import_series_folder_adoption(
            session,
            job,
            item,
            importable_files,
            resolved_series_id=resolved_series_id,
            ingest_policy=ingest_policy,
            record_action=record_action,
            log_event=log_event,
        )

    placeholder_progress_live_only = False
    if _setup_placeholder_targets:
        placeholder_progress_live_only = await _ensure_placeholder_issue_targets(
            session,
            series_id=resolved_series_id,
            importable_files=importable_files,
        )

    effective_worker_count = max(int(file_worker_count or 1), 1)
    parallel_processing_available = (
        _file_ids_override is None
        and session_factory is not None
        and effective_worker_count > 1
        and len(importable_file_ids) > 1
    )
    if parallel_processing_available and not await _requires_serial_file_processing(
        session,
        job,
        resolved_series_id=resolved_series_id,
        importable_files=importable_files,
    ):
        assert session_factory is not None
        await session.commit()
        file_index_by_id = {
            int(imp_file_id): index
            for index, imp_file_id in enumerate(importable_file_ids, start=1)
        }
        record_action_lock = asyncio.Lock()
        next_action_sequence = int(
            await session.scalar(
                sa_select(sa_func.max(ImportJobAction.sequence_no)).where(
                    ImportJobAction.import_job_id == job_id
                )
            )
            or 0
        )
        semaphore = asyncio.Semaphore(min(effective_worker_count, len(importable_file_ids)))

        async def locked_record_action(
            session: AsyncSession,
            job: ImportJob,
            *,
            phase: str,
            action_type: str,
            payload: dict[str, Any],
        ) -> ImportJobAction:
            nonlocal next_action_sequence
            async with record_action_lock:
                next_action_sequence += 1
                action = await record_action(
                    session,
                    job,
                    phase=phase,
                    action_type=action_type,
                    payload=payload,
                )
                action.sequence_no = next_action_sequence
                await session.flush()
                return action

        async def process_one_file(imp_file_id: int) -> tuple[int, int]:
            async with semaphore, session_factory() as worker_session:
                worker_job = await worker_session.get(type(job), job_id)
                worker_item = await worker_session.get(type(item), item_id)
                if worker_job is None or worker_item is None:
                    raise ValueError("Import job or series disappeared during file processing")
                return await process_import_series_files(
                    worker_session,
                    worker_job,
                    worker_item,
                    duplicate_mode=duplicate_mode,
                    series_id_override=resolved_series_id,
                    load_media_settings=load_media_settings,
                    load_trash_dir=load_trash_dir,
                    load_ingest_policy=load_ingest_policy,
                    load_permission_policy=load_permission_policy,
                    raise_if_cancelled=raise_if_cancelled,
                    prepare_file=prepare_file,
                    build_comicinfo_payload=build_comicinfo_payload,
                    apply_comicinfo=apply_comicinfo,
                    cleanup_prepared_file=cleanup_prepared_file,
                    record_action=locked_record_action,
                    log_event=log_event,
                    register_file=register_file,
                    move_to_trash=move_to_trash,
                    report_file_progress=report_file_progress,
                    defer_comicinfo_enrichment=defer_comicinfo_enrichment,
                    file_worker_count=1,
                    session_factory=None,
                    _file_ids_override=[imp_file_id],
                    _file_index_by_id=file_index_by_id,
                    _total_file_count=len(importable_file_ids),
                    _update_item_counters=False,
                    _setup_placeholder_targets=False,
                )

        results = await asyncio.gather(
            *(process_one_file(int(imp_file_id)) for imp_file_id in importable_file_ids)
        )
        files_imported = sum(imported for imported, _failed in results)
        files_failed = sum(failed for _imported, failed in results)
        reloaded_item = await session.get(type(item), item_id)
        if reloaded_item is not None:
            item = reloaded_item
        if _update_item_counters:
            files_safety_blocked = int(
                await session.scalar(
                    sa_select(sa_func.count())
                    .select_from(ImportedFile)
                    .where(
                        ImportedFile.import_series_id == item_id,
                        ImportedFile.status == ImportedFileStatus.SAFETY_BLOCKED,
                    )
                )
                or 0
            )
            item.files_imported = files_imported
            item.files_failed = files_failed
            diagnostics = dict(item.diagnostics or {})
            if files_safety_blocked:
                diagnostics["safety_blocked_files"] = files_safety_blocked
            else:
                diagnostics.pop("safety_blocked_files", None)
            item.diagnostics = diagnostics
            await session.flush()
        return files_imported, files_failed

    cv_id_to_issue, number_to_issue = await load_issue_lookup_for_series(
        session,
        resolved_series_id,
    )
    cv_id_to_issue_id = {
        comicvine_id: issue.id
        for comicvine_id, issue in cv_id_to_issue.items()
        if issue.id is not None
    }
    number_to_issue_id = {
        issue_number: issue.id
        for issue_number, issue in number_to_issue.items()
        if issue.id is not None
    }

    media_settings = await load_media_settings(session, job)
    skip_existing_enabled = media_settings["skip_existing_files"].lower() == "true"
    trash_dir = await load_trash_dir(session, job)
    permission_policy = await load_permission_policy(session, job)
    trash_dir.mkdir(parents=True, exist_ok=True)
    issue_ids = {
        issue.id for issue in [*cv_id_to_issue.values(), *number_to_issue.values()] if issue.id
    }
    owned_issue_ids = (
        await _load_owned_issue_ids(session, issue_ids) if skip_existing_enabled else set()
    )
    comicinfo_payload_cache: dict[tuple[int, str], dict[str, Any]] = {}

    files_imported = 0
    files_failed = 0
    files_safety_blocked = 0

    total_importable_files = _total_file_count or len(importable_file_ids)

    for local_file_index, imp_file_id in enumerate(importable_file_ids, start=1):
        file_index = (
            _file_index_by_id.get(int(imp_file_id), local_file_index)
            if _file_index_by_id is not None
            else local_file_index
        )
        prepared: PreparedImportFile | None = None
        placed_destination_path: Path | None = None
        placed_series_folder_created = False
        placed_series_folder_path: Path | None = None
        original_trash_path: Path | None = None
        imp_file = await _load_imported_file_for_processing(session, imp_file_id)
        if imp_file is None:
            continue
        current_job = await session.get(type(job), job_id)
        if current_job is None:
            raise ValueError("Import job disappeared during file processing")
        imp_file_name = imp_file.file_name
        imp_file_path = imp_file.file_path
        try:
            await raise_if_cancelled(session, job_id)

            def _build_current_file_reporter(
                current_imp_file: ImportedFile,
                current_file_index: int,
                *,
                force_live_only: bool,
            ) -> Callable[[str, int, int, str], Awaitable[None]]:
                async def report_current_file(
                    stage: str,
                    current: int,
                    total: int,
                    unit: str,
                ) -> None:
                    if report_file_progress is None:
                        return
                    await report_file_progress(
                        imp_file=current_imp_file,
                        file_index=current_file_index,
                        total_files=total_importable_files,
                        stage=stage,
                        current=current,
                        total=total,
                        unit=unit,
                        live_only=force_live_only,
                    )

                return report_current_file

            force_live_file_progress = placeholder_progress_live_only
            _report_current_file = _build_current_file_reporter(
                imp_file,
                file_index,
                force_live_only=force_live_file_progress,
            )

            if report_file_progress is not None:
                await report_file_progress(
                    imp_file=imp_file,
                    file_index=file_index,
                    total_files=total_importable_files,
                    stage="preparing",
                    current=0,
                    total=1,
                    unit="steps",
                    live_only=force_live_file_progress,
                )
            resolved_issue_id = _resolve_import_file_issue_id(
                imp_file,
                cv_id_to_issue_id=cv_id_to_issue_id,
                number_to_issue_id=number_to_issue_id,
            )
            if resolved_issue_id is None:
                imp_file.status = ImportedFileStatus.FAILED
                imp_file.error_message = "Could not resolve to a library issue"
                files_failed += 1
                await session.flush()
                await session.commit()
                placeholder_progress_live_only = False
                continue
            resolved_issue = await _load_issue_for_processing(session, resolved_issue_id)
            if resolved_issue is None:
                imp_file.status = ImportedFileStatus.FAILED
                imp_file.error_message = "Could not resolve to a library issue"
                files_failed += 1
                await session.flush()
                await session.commit()
                placeholder_progress_live_only = False
                continue

            confidence = _MATCH_CONFIDENCE_MAP.get(
                imp_file.match_confidence or "", MatchConfidence.MEDIUM
            )

            if skip_existing_enabled and resolved_issue.id in owned_issue_ids:
                imp_file.status = ImportedFileStatus.SKIPPED
                await log_event(
                    session,
                    job.id,
                    "INFO",
                    "import_file_skipped_existing",
                    message=f"Skipped (issue already in library): {imp_file.file_name}",
                )
                await session.commit()
                placeholder_progress_live_only = False
                continue

            comicinfo_payload: dict[str, Any] | None = None
            with session.no_autoflush:
                try:
                    prepared = await prepare_file(
                        session,
                        current_job,
                        imp_file,
                        progress_callback=_report_current_file if report_file_progress else None,
                    )
                except Exception as exc:
                    resource_block = classify_resource_safety_exception(exc)
                    if resource_block is None:
                        raise
                    await session.rollback()
                    persisted_file = await session.get(ImportedFile, imp_file_id)
                    if persisted_file is None:
                        raise
                    imp_file = persisted_file
                    reloaded_item = await session.get(type(item), item_id)
                    if reloaded_item is not None:
                        item = reloaded_item
                    diagnostics = dict(imp_file.diagnostics or {})
                    diagnostics["safety_block"] = resource_block.to_diagnostics()
                    imp_file.status = ImportedFileStatus.SAFETY_BLOCKED
                    imp_file.include_in_import = False
                    imp_file.error_message = resource_block.reason
                    imp_file.diagnostics = diagnostics
                    files_safety_blocked += 1
                    await log_event(
                        session,
                        job_id,
                        "WARNING",
                        "import_file_safety_blocked",
                        message=f"File needs safety review before retry: {imp_file_name}",
                        source_path=imp_file_path,
                        safety_kind=resource_block.kind,
                        error=resource_block.reason,
                    )
                    await session.commit()
                    placeholder_progress_live_only = False
                    continue
                effective_embedded_comicinfo = update_embedded_comicinfo_from_match and not bool(
                    getattr(prepared, "skip_embedded_comicinfo", False)
                )
                preparation_warning = getattr(prepared, "preparation_warning", None)
                if preparation_warning:
                    await log_event(
                        session,
                        job_id,
                        "WARNING",
                        "import_file_normalization_skipped_for_safety",
                        message=preparation_warning,
                        source_path=imp_file.file_path,
                    )
                if prepared.converted:
                    await log_event(
                        session,
                        job_id,
                        "DEBUG",
                        "import_file_converted_to_cbz",
                        message=f"Converted to CBZ: {Path(prepared.registration_source).name}",
                        source_path=str(prepared.original_source),
                        prepared_path=str(prepared.registration_source),
                        source_file_name=Path(prepared.original_source).name,
                        prepared_file_name=Path(prepared.registration_source).name,
                    )
                if effective_embedded_comicinfo:
                    prepared_source_path = Path(prepared.registration_source)
                    comicinfo_payload_cache_key = (resolved_issue.id, str(prepared_source_path))
                    comicinfo_payload = comicinfo_payload_cache.get(comicinfo_payload_cache_key)
                    if comicinfo_payload is None:
                        comicinfo_payload = await _build_comicinfo_payload_with_progress(
                            session,
                            resolved_issue,
                            source_path=prepared_source_path,
                            build_comicinfo_payload=build_comicinfo_payload,
                            report_file_progress=report_file_progress,
                            imp_file=imp_file,
                            file_index=file_index,
                            total_files=total_importable_files,
                            defer_issue_enrichment=defer_comicinfo_enrichment,
                        )
                        comicinfo_payload_cache[comicinfo_payload_cache_key] = comicinfo_payload

                registration_result = await register_file(
                    session,
                    Path(prepared.registration_source),
                    resolved_issue,
                    confidence,
                    move_to_library=move_to_library,
                    library_root_id=target_library_root_id,
                    transfer_method=transfer_method,
                    normalize_to_cbz=False,
                    update_embedded_comicinfo_from_match=effective_embedded_comicinfo,
                    comicinfo_payload=comicinfo_payload,
                    loaded_issue=resolved_issue,
                    ingest_policy=ingest_policy,
                    permission_policy=permission_policy,
                    transfer_progress_callback=(
                        _report_current_file if report_file_progress else None
                    ),
                    comicinfo_progress_callback=(
                        _report_current_file if report_file_progress else None
                    ),
                )
            library_file, registration = _registration_outcome(registration_result)
            placed_destination_path = Path(library_file.file_path)
            placed_series_folder_created = (
                bool(registration.series_folder_created) if registration is not None else False
            )
            placed_series_folder_path = (
                registration.series_folder_path
                if registration is not None
                else placed_destination_path.parent
            )
            final_file_name = placed_destination_path.name
            if comicinfo_payload is not None:
                await log_event(
                    session,
                    job_id,
                    "DEBUG",
                    "import_file_comicinfo_updated",
                    message=f"ComicInfo.xml written: {final_file_name}",
                    source_path=imp_file.file_path,
                    destination_path=library_file.file_path,
                    destination_file_name=final_file_name,
                    library_file_id=library_file.id,
                    issue_id=resolved_issue.id,
                )
            if report_file_progress is not None:
                await report_file_progress(
                    imp_file=imp_file,
                    file_index=file_index,
                    total_files=total_importable_files,
                    stage="finalizing",
                    current=0,
                    total=4,
                    unit="steps",
                    live_only=True,
                )
            if prepared.converted and transfer_method == "move":
                original_trash_path = await asyncio.to_thread(
                    move_to_trash,
                    prepared.original_source,
                    trash_dir,
                    relative_path=Path("imports") / prepared.original_source.name,
                )
            if report_file_progress is not None:
                await report_file_progress(
                    imp_file=imp_file,
                    file_index=file_index,
                    total_files=total_importable_files,
                    stage="finalizing",
                    current=1,
                    total=4,
                    unit="steps",
                    live_only=True,
                )
            imp_file.matched_issue_id = resolved_issue.id
            imp_file.library_file_id = library_file.id
            imp_file.status = ImportedFileStatus.IMPORTED
            comicinfo_enrichment_deferred = (
                bool(defer_comicinfo_enrichment)
                and comicinfo_payload is not None
                and resolved_issue.comicvine_id is not None
            )
            if comicinfo_enrichment_deferred:
                diagnostics = dict(imp_file.diagnostics or {})
                diagnostics[_COMICINFO_ENRICHMENT_DIAGNOSTIC_KEY] = {
                    "status": "pending",
                    "reason": "deferred_during_import",
                    "issue_id": resolved_issue.id,
                    "issue_cv_id": resolved_issue.comicvine_id,
                    "library_file_id": library_file.id,
                    "queued_at": datetime.now(UTC).isoformat(),
                }
                imp_file.diagnostics = diagnostics
                await log_event(
                    session,
                    job_id,
                    "DEBUG",
                    "import_file_comicinfo_enrichment_deferred",
                    message=f"Deferred full ComicInfo metadata refresh: {final_file_name}",
                    source_path=imp_file.file_path,
                    destination_path=library_file.file_path,
                    library_file_id=library_file.id,
                    issue_id=resolved_issue.id,
                    issue_cv_id=resolved_issue.comicvine_id,
                )
            if report_file_progress is not None:
                await report_file_progress(
                    imp_file=imp_file,
                    file_index=file_index,
                    total_files=total_importable_files,
                    stage="finalizing",
                    current=2,
                    total=4,
                    unit="steps",
                    live_only=True,
                )
            await record_action(
                session,
                current_job,
                phase="import",
                action_type="library_file_registered",
                payload={
                    "imported_file_id": imp_file.id,
                    "library_file_id": library_file.id,
                    "destination_path": library_file.file_path,
                    "original_source_path": str(prepared.original_source),
                    "transfer_method": (transfer_method if move_to_library else "leave_in_place"),
                    "original_trash_path": (
                        str(original_trash_path) if original_trash_path is not None else ""
                    ),
                    "converted": prepared.converted,
                    "embedded_comicinfo_updated": comicinfo_payload is not None,
                    "embedded_comicinfo_enrichment_deferred": comicinfo_enrichment_deferred,
                    "created_series_folder": (
                        bool(registration.series_folder_created)
                        if registration is not None
                        else False
                    ),
                    "created_series_folder_path": (
                        str(registration.series_folder_path)
                        if registration is not None and registration.series_folder_path is not None
                        else ""
                    ),
                    "permission_restores": _permission_restore_payload(
                        registration,
                        original_source=prepared.original_source,
                        transfer_method=(transfer_method if move_to_library else "leave_in_place"),
                    ),
                },
            )

            await log_event(
                session,
                job_id,
                "DEBUG",
                "import_file_placed",
                message=f"File placed: {final_file_name}",
                source_path=imp_file.file_path,
                destination_path=library_file.file_path,
                destination_file_name=final_file_name,
                library_file_id=library_file.id,
                issue_id=resolved_issue.id,
            )
            # Make the imported file durable and release SQLite's writer slot
            # before removing potentially large conversion workspaces.
            await session.commit()
            placeholder_progress_live_only = False
            try:
                await asyncio.to_thread(cleanup_prepared_file, prepared)
            except Exception:
                logger.exception(
                    "import_file_post_commit_workspace_cleanup_failed",
                    file_path=imp_file_path,
                    destination_path=library_file.file_path,
                )
            if report_file_progress is not None:
                try:
                    await report_file_progress(
                        imp_file=imp_file,
                        file_index=file_index,
                        total_files=total_importable_files,
                        stage="finalizing",
                        current=3,
                        total=4,
                        unit="steps",
                        live_only=True,
                    )
                except Exception:
                    logger.exception(
                        "import_file_post_commit_progress_failed",
                        file_path=imp_file_path,
                        progress_current=3,
                    )
            owned_issue_ids.add(resolved_issue.id)
            files_imported += 1
            if report_file_progress is not None:
                # Emit the terminal file-progress checkpoint only after the
                # import-row/library-file transaction commits. Persisting this
                # event through the dedicated progress session while the main
                # session still holds uncommitted writes can deadlock SQLite.
                try:
                    await report_file_progress(
                        imp_file=imp_file,
                        file_index=file_index,
                        total_files=total_importable_files,
                        stage="finalizing",
                        current=4,
                        total=4,
                        unit="steps",
                    )
                except Exception:
                    logger.exception(
                        "import_file_post_commit_progress_failed",
                        file_path=imp_file_path,
                        progress_current=4,
                    )

        except (JobPausedError, JobCancelledError):
            if prepared is not None:
                await asyncio.to_thread(cleanup_prepared_file, prepared)
            raise
        except Exception as exc:
            if prepared is not None:
                await asyncio.to_thread(cleanup_prepared_file, prepared)
            await session.rollback()
            placeholder_progress_live_only = False
            try:
                await asyncio.to_thread(
                    _cleanup_failed_library_artifact,
                    destination_path=placed_destination_path,
                    original_source=prepared.original_source if prepared is not None else None,
                    original_trash_path=original_trash_path,
                    transfer_method=transfer_method,
                    created_series_folder=placed_series_folder_created,
                    created_series_folder_path=placed_series_folder_path,
                )
            except Exception:
                logger.exception(
                    "import_file_failed_cleanup_error",
                    file_path=imp_file_path,
                    destination_path=(
                        str(placed_destination_path)
                        if placed_destination_path is not None
                        else None
                    ),
                )
            persisted_file = await session.get(ImportedFile, imp_file_id)
            if persisted_file is None:
                raise
            imp_file = persisted_file
            reloaded_item = await session.get(type(item), item_id)
            if reloaded_item is not None:
                item = reloaded_item
            logger.debug(
                "import_file_failed",
                file_path=imp_file_path,
                error=str(exc),
            )
            resource_block = classify_resource_safety_exception(exc)
            if resource_block is not None:
                diagnostics = dict(imp_file.diagnostics or {})
                diagnostics["safety_block"] = resource_block.to_diagnostics()
                imp_file.status = ImportedFileStatus.SAFETY_BLOCKED
                imp_file.include_in_import = False
                imp_file.error_message = resource_block.reason
                imp_file.diagnostics = diagnostics
                files_safety_blocked += 1
                await log_event(
                    session,
                    job_id,
                    "WARNING",
                    "import_file_safety_blocked",
                    message=f"File needs safety review before retry: {imp_file_name}",
                    source_path=imp_file_path,
                    safety_kind=resource_block.kind,
                    error=resource_block.reason,
                )
                await session.commit()
                continue

            imp_file.status = ImportedFileStatus.FAILED
            imp_file.error_message = str(exc)
            files_failed += 1

            await log_event(
                session,
                job_id,
                "DEBUG",
                "import_file_place_failed",
                message=f"File placement failed: {imp_file_name}",
                source_path=imp_file_path,
                error=str(exc),
            )
            await session.commit()

    if _update_item_counters:
        item.files_imported = files_imported
        item.files_failed = files_failed
        diagnostics = dict(item.diagnostics or {})
        if files_safety_blocked:
            diagnostics["safety_blocked_files"] = files_safety_blocked
        else:
            diagnostics.pop("safety_blocked_files", None)
        item.diagnostics = diagnostics
        await session.flush()

    return files_imported, files_failed


async def _load_owned_issue_ids(
    session: AsyncSession,
    issue_ids: set[int],
) -> set[int]:
    if not issue_ids:
        return set()

    owned_result = await session.execute(
        sa_select(LibraryFile.issue_id).where(LibraryFile.issue_id.in_(issue_ids))
    )
    return {issue_id for issue_id in owned_result.scalars().all() if issue_id is not None}
