"""Background ComicInfo enrichment for fast collection imports."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select as sa_select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import joinedload

from pullbox.core.sqlite_lock import (
    SQLITE_LOCK_RETRY_ATTEMPTS,
    is_sqlite_locked_error,
    sqlite_lock_retry_delay,
)
from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportJob, ImportJobStatus
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile
from pullbox.models.series import Series
from pullbox.services.import_comicinfo_metadata import is_retryable_provider_error

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)

COMICINFO_ENRICHMENT_DIAGNOSTIC_KEY = "comicinfo_enrichment"

ImportComicInfoBuildPayload = Callable[..., Awaitable[dict[str, Any]]]
ImportComicInfoApply = Callable[[Path, dict[str, Any]], Any]
ImportComicInfoLogEvent = Callable[..., Awaitable[None]]

comicinfo_enrichment_tasks: set[asyncio.Task[None]] = set()
_comicinfo_enrichment_semaphore: asyncio.Semaphore | None = None
_comicinfo_enrichment_semaphore_loop: asyncio.AbstractEventLoop | None = None


@dataclass(frozen=True)
class PreparedComicInfoEnrichment:
    """Database-independent inputs for one deferred archive rewrite."""

    artifact_path: Path
    payload: dict[str, Any]
    library_file_id: int
    library_file_name: str
    issue_id: int
    issue_cv_id: int | None


def reset_comicinfo_enrichment_gate() -> None:
    """Reset the app-local ComicInfo enrichment lane for tests and loop restarts."""
    global _comicinfo_enrichment_semaphore, _comicinfo_enrichment_semaphore_loop
    _comicinfo_enrichment_semaphore = None
    _comicinfo_enrichment_semaphore_loop = None


def comicinfo_enrichment_gate() -> asyncio.Semaphore:
    """Return the app-local lane for deferred ComicInfo metadata refreshes."""
    global _comicinfo_enrichment_semaphore, _comicinfo_enrichment_semaphore_loop

    loop = asyncio.get_running_loop()
    if _comicinfo_enrichment_semaphore is None or _comicinfo_enrichment_semaphore_loop is not loop:
        _comicinfo_enrichment_semaphore = asyncio.Semaphore(1)
        _comicinfo_enrichment_semaphore_loop = loop
    return _comicinfo_enrichment_semaphore


def schedule_import_comicinfo_enrichment(
    session_factory: async_sessionmaker[AsyncSession] | None,
    *,
    job_id: int,
    build_comicinfo_payload: ImportComicInfoBuildPayload,
    apply_comicinfo: ImportComicInfoApply,
    log_event: ImportComicInfoLogEvent,
) -> None:
    """Queue deferred issue metadata and ComicInfo rewrites after Step 4 completes."""
    if session_factory is None:
        return

    async def run_enrichment() -> None:
        try:
            await run_import_comicinfo_enrichment(
                session_factory,
                job_id=job_id,
                build_comicinfo_payload=build_comicinfo_payload,
                apply_comicinfo=apply_comicinfo,
                log_event=log_event,
            )
        except Exception as exc:
            logger.warning(
                "import_comicinfo_enrichment_failed",
                job_id=job_id,
                error=str(exc),
            )

    task = asyncio.create_task(run_enrichment())
    comicinfo_enrichment_tasks.add(task)
    task.add_done_callback(comicinfo_enrichment_tasks.discard)


async def run_pending_import_comicinfo_enrichment(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    build_comicinfo_payload: ImportComicInfoBuildPayload,
    apply_comicinfo: ImportComicInfoApply,
    log_event: ImportComicInfoLogEvent,
) -> int:
    """Refresh deferred ComicInfo metadata for all completed jobs with pending rows."""
    pending_job_ids = await _load_pending_import_job_ids(session_factory)
    for job_id in pending_job_ids:
        await run_import_comicinfo_enrichment(
            session_factory,
            job_id=job_id,
            build_comicinfo_payload=build_comicinfo_payload,
            apply_comicinfo=apply_comicinfo,
            log_event=log_event,
        )
    return len(pending_job_ids)


async def run_import_comicinfo_enrichment(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id: int,
    build_comicinfo_payload: ImportComicInfoBuildPayload,
    apply_comicinfo: ImportComicInfoApply,
    log_event: ImportComicInfoLogEvent,
) -> None:
    """Refresh deferred ComicInfo metadata for imported files in one import job."""
    async with comicinfo_enrichment_gate():
        await _run_import_comicinfo_enrichment_while_fenced(
            session_factory,
            job_id=job_id,
            build_comicinfo_payload=build_comicinfo_payload,
            apply_comicinfo=apply_comicinfo,
            log_event=log_event,
        )


async def _run_import_comicinfo_enrichment_while_fenced(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id: int,
    build_comicinfo_payload: ImportComicInfoBuildPayload,
    apply_comicinfo: ImportComicInfoApply,
    log_event: ImportComicInfoLogEvent,
) -> None:
    """Run one job while holding the process-local filesystem mutation fence."""
    if not await _import_job_is_completed(session_factory, job_id=job_id):
        return
    pending_ids = await _load_pending_imported_file_ids(session_factory, job_id=job_id)
    for imported_file_id in pending_ids:
        try:
            prepared = await _prepare_pending_imported_file_with_retry(
                session_factory,
                imported_file_id=imported_file_id,
                build_comicinfo_payload=build_comicinfo_payload,
            )
            if prepared is None:
                continue
            if not await _import_job_is_completed(session_factory, job_id=job_id):
                return

            if inspect.iscoroutinefunction(apply_comicinfo):
                await apply_comicinfo(prepared.artifact_path, prepared.payload)
            else:
                apply_result = await asyncio.to_thread(
                    apply_comicinfo,
                    prepared.artifact_path,
                    prepared.payload,
                )
                if inspect.isawaitable(apply_result):
                    await apply_result

            await _mark_pending_file_complete_with_retry(
                session_factory,
                job_id=job_id,
                imported_file_id=imported_file_id,
                prepared=prepared,
                log_event=log_event,
            )
        except Exception as exc:
            if is_retryable_provider_error(exc):
                logger.warning(
                    "import_comicinfo_enrichment_deferred_for_provider",
                    job_id=job_id,
                    imported_file_id=imported_file_id,
                    error=str(exc),
                )
                break
            await _mark_pending_file_failed(
                session_factory,
                job_id=job_id,
                imported_file_id=imported_file_id,
                error=str(exc),
                log_event=log_event,
            )


async def _import_job_is_completed(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id: int,
) -> bool:
    """Read durable job state at a filesystem safe boundary."""
    async with session_factory() as session:
        status = await session.scalar(sa_select(ImportJob.status).where(ImportJob.id == job_id))
        await session.rollback()
    return status is ImportJobStatus.COMPLETED


async def _load_pending_imported_file_ids(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id: int,
) -> list[int]:
    async with session_factory() as session:
        result = await session.execute(
            sa_select(ImportedFile.id)
            .join(ImportJob, ImportedFile.import_job_id == ImportJob.id)
            .where(ImportedFile.import_job_id == job_id)
            .where(ImportJob.status == ImportJobStatus.COMPLETED)
            .where(ImportedFile.status == ImportedFileStatus.IMPORTED)
        )
        ids: list[int] = []
        for imported_file_id in result.scalars().all():
            imported_file = await session.get(ImportedFile, imported_file_id)
            if _is_pending_comicinfo_enrichment(imported_file):
                ids.append(int(imported_file_id))
        return ids


async def _load_pending_import_job_ids(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[int]:
    async with session_factory() as session:
        result = await session.execute(
            sa_select(ImportedFile.import_job_id, ImportedFile.diagnostics)
            .join(ImportJob, ImportedFile.import_job_id == ImportJob.id)
            .where(ImportJob.status == ImportJobStatus.COMPLETED)
            .where(ImportedFile.status == ImportedFileStatus.IMPORTED)
            .order_by(ImportedFile.import_job_id)
        )
        ids: list[int] = []
        seen: set[int] = set()
        for job_id, diagnostics in result.all():
            if int(job_id) in seen or not _is_pending_comicinfo_enrichment_diagnostics(diagnostics):
                continue
            seen.add(int(job_id))
            ids.append(int(job_id))
        return ids


def _is_pending_comicinfo_enrichment(imported_file: ImportedFile | None) -> bool:
    if imported_file is None:
        return False
    return _is_pending_comicinfo_enrichment_diagnostics(imported_file.diagnostics)


def _is_pending_comicinfo_enrichment_diagnostics(diagnostics: Any) -> bool:
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    details = diagnostics.get(COMICINFO_ENRICHMENT_DIAGNOSTIC_KEY)
    return isinstance(details, dict) and details.get("status") == "pending"


async def _prepare_pending_imported_file_with_retry(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    imported_file_id: int,
    build_comicinfo_payload: ImportComicInfoBuildPayload,
) -> PreparedComicInfoEnrichment | None:
    """Prepare metadata and commit it before any archive mutation begins."""
    for attempt in range(1, SQLITE_LOCK_RETRY_ATTEMPTS + 1):
        async with session_factory() as session:
            try:
                prepared = await _prepare_pending_imported_file(
                    session,
                    imported_file_id=imported_file_id,
                    build_comicinfo_payload=build_comicinfo_payload,
                )
                await session.commit()
                return prepared
            except OperationalError as exc:
                await session.rollback()
                if not is_sqlite_locked_error(exc):
                    raise
                if attempt == SQLITE_LOCK_RETRY_ATTEMPTS:
                    logger.warning(
                        "import_comicinfo_prepare_deferred_after_sqlite_lock",
                        imported_file_id=imported_file_id,
                        attempts=attempt,
                    )
                    return None
                delay_seconds = sqlite_lock_retry_delay(attempt)
                logger.warning(
                    "import_comicinfo_prepare_retrying_after_sqlite_lock",
                    imported_file_id=imported_file_id,
                    attempt=attempt,
                    max_attempts=SQLITE_LOCK_RETRY_ATTEMPTS,
                    delay_seconds=delay_seconds,
                )
            except Exception:
                await session.rollback()
                raise
        await asyncio.sleep(delay_seconds)
    return None


async def _prepare_pending_imported_file(
    session: AsyncSession,
    *,
    imported_file_id: int,
    build_comicinfo_payload: ImportComicInfoBuildPayload,
) -> PreparedComicInfoEnrichment | None:
    imported_file = await session.get(ImportedFile, imported_file_id)
    if not _is_pending_comicinfo_enrichment(imported_file):
        return None
    assert imported_file is not None
    job_status = await session.scalar(
        sa_select(ImportJob.status).where(ImportJob.id == imported_file.import_job_id)
    )
    if job_status is not ImportJobStatus.COMPLETED:
        return None

    library_file_id = imported_file.library_file_id
    if library_file_id is None:
        details = imported_file.diagnostics.get(COMICINFO_ENRICHMENT_DIAGNOSTIC_KEY, {})
        if isinstance(details, dict):
            library_file_id = details.get("library_file_id")
    if library_file_id is None:
        raise ValueError("Deferred ComicInfo enrichment is missing library_file_id")

    library_file = await session.get(LibraryFile, int(library_file_id))
    if library_file is None:
        raise ValueError(f"Library file {library_file_id} no longer exists")

    issue_id = imported_file.matched_issue_id or library_file.issue_id
    if issue_id is None:
        raise ValueError("Deferred ComicInfo enrichment is missing issue_id")

    issue_result = await session.execute(
        sa_select(Issue)
        .options(joinedload(Issue.series).joinedload(Series.publisher))
        .where(Issue.id == int(issue_id))
    )
    issue = issue_result.scalars().first()
    if issue is None:
        raise ValueError(f"Issue {issue_id} no longer exists")

    artifact_path = Path(library_file.file_path)
    payload = await build_comicinfo_payload(
        session,
        issue,
        source_path=artifact_path,
        defer_issue_enrichment=False,
        propagate_retryable_provider_errors=True,
    )
    return PreparedComicInfoEnrichment(
        artifact_path=artifact_path,
        payload=payload,
        library_file_id=library_file.id,
        library_file_name=library_file.file_name,
        issue_id=issue.id,
        issue_cv_id=issue.comicvine_id,
    )


async def _mark_pending_file_complete_with_retry(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id: int,
    imported_file_id: int,
    prepared: PreparedComicInfoEnrichment,
    log_event: ImportComicInfoLogEvent,
) -> bool:
    """Persist archive completion in a short, retryable transaction."""
    for attempt in range(1, SQLITE_LOCK_RETRY_ATTEMPTS + 1):
        async with session_factory() as session:
            try:
                imported_file = await session.get(ImportedFile, imported_file_id)
                if not _is_pending_comicinfo_enrichment(imported_file):
                    return True
                assert imported_file is not None

                library_file = await session.get(LibraryFile, prepared.library_file_id)
                if library_file is None:
                    raise ValueError(f"Library file {prepared.library_file_id} no longer exists")
                try:
                    artifact_stat = await asyncio.to_thread(prepared.artifact_path.stat)
                except FileNotFoundError:
                    artifact_stat = None
                if artifact_stat is not None:
                    library_file.file_size = artifact_stat.st_size
                    library_file.file_modified_at = datetime.fromtimestamp(
                        artifact_stat.st_mtime,
                        tz=UTC,
                    )
                _set_comicinfo_enrichment_status(
                    imported_file,
                    status="complete",
                    completed_at=datetime.now(UTC).isoformat(),
                )
                await log_event(
                    session,
                    job_id,
                    "DEBUG",
                    "import_file_comicinfo_enrichment_completed",
                    message=(
                        f"Deferred ComicInfo metadata refreshed: {prepared.library_file_name}"
                    ),
                    destination_path=str(prepared.artifact_path),
                    library_file_id=prepared.library_file_id,
                    issue_id=prepared.issue_id,
                    issue_cv_id=prepared.issue_cv_id,
                )
                await session.commit()
                return True
            except OperationalError as exc:
                await session.rollback()
                if not is_sqlite_locked_error(exc):
                    raise
                if attempt == SQLITE_LOCK_RETRY_ATTEMPTS:
                    logger.warning(
                        "import_comicinfo_completion_deferred_after_sqlite_lock",
                        imported_file_id=imported_file_id,
                        attempts=attempt,
                    )
                    return False
                delay_seconds = sqlite_lock_retry_delay(attempt)
                logger.warning(
                    "import_comicinfo_completion_retrying_after_sqlite_lock",
                    imported_file_id=imported_file_id,
                    attempt=attempt,
                    max_attempts=SQLITE_LOCK_RETRY_ATTEMPTS,
                    delay_seconds=delay_seconds,
                )
            except Exception:
                await session.rollback()
                raise
        await asyncio.sleep(delay_seconds)
    return False


async def _mark_pending_file_failed(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id: int,
    imported_file_id: int,
    error: str,
    log_event: ImportComicInfoLogEvent,
) -> bool:
    """Record a non-transient failure without letting a lock abort the queue."""
    for attempt in range(1, SQLITE_LOCK_RETRY_ATTEMPTS + 1):
        async with session_factory() as session:
            try:
                imported_file = await session.get(ImportedFile, imported_file_id)
                if imported_file is None:
                    return True
                _set_comicinfo_enrichment_status(
                    imported_file,
                    status="failed",
                    error=error,
                    failed_at=datetime.now(UTC).isoformat(),
                )
                await log_event(
                    session,
                    job_id,
                    "WARNING",
                    "import_file_comicinfo_enrichment_failed",
                    message=(
                        f"Deferred ComicInfo metadata refresh failed: {imported_file.file_name}"
                    ),
                    imported_file_id=imported_file.id,
                    error=error,
                )
                await session.commit()
                return True
            except OperationalError as exc:
                await session.rollback()
                if not is_sqlite_locked_error(exc):
                    raise
                if attempt == SQLITE_LOCK_RETRY_ATTEMPTS:
                    logger.warning(
                        "import_comicinfo_failure_status_deferred_after_sqlite_lock",
                        imported_file_id=imported_file_id,
                        attempts=attempt,
                    )
                    return False
                delay_seconds = sqlite_lock_retry_delay(attempt)
                logger.warning(
                    "import_comicinfo_failure_status_retrying_after_sqlite_lock",
                    imported_file_id=imported_file_id,
                    attempt=attempt,
                    max_attempts=SQLITE_LOCK_RETRY_ATTEMPTS,
                    delay_seconds=delay_seconds,
                )
            except Exception:
                await session.rollback()
                raise
        await asyncio.sleep(delay_seconds)
    return False


def _set_comicinfo_enrichment_status(
    imported_file: ImportedFile,
    *,
    status: str,
    **updates: Any,
) -> None:
    diagnostics = dict(imported_file.diagnostics or {})
    details = dict(diagnostics.get(COMICINFO_ENRICHMENT_DIAGNOSTIC_KEY) or {})
    details.update({"status": status, **updates})
    diagnostics[COMICINFO_ENRICHMENT_DIAGNOSTIC_KEY] = details
    imported_file.diagnostics = diagnostics
