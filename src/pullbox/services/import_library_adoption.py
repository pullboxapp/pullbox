"""Build a clean managed library from a completed reference-only import."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Final

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import delete, func, insert, select

from pullbox.core.config_resolver import get_application_secret
from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.core.library_policy import load_effective_library_ingest_policy
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportFileHandlingMode,
    ImportJob,
    ImportJobLog,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile, LibraryFileStorageMode, LibraryRoot
from pullbox.models.series import Series
from pullbox.schemas.import_job import ImportProgressEvent
from pullbox.services.import_completed_cleanup import (
    CompletedImportCleanupAction,
    count_completed_import_cleanup_scope,
)
from pullbox.services.import_policy_snapshot import apply_ingest_policy_to_import_job
from pullbox.services.import_workflow_state import (
    emit_progress,
    phase_progress,
    raise_if_job_cancelled,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement


_TOKEN_SALT: Final = "completed-import-clean-library-v1"
_TOKEN_MAX_AGE_SECONDS: Final = 15 * 60
_TOKEN_VERSION: Final = 1
_SERIES_BATCH_SIZE: Final = 500
_FILE_BATCH_SIZE: Final = 2_000
_PREPARATION_PROGRESS_END: Final = 5


@dataclass(frozen=True, slots=True)
class CleanLibraryImportPreview:
    """Exact source-preserving adoption scope shown before job creation."""

    source_job_id: int
    target_root_id: int
    eligible_file_count: int
    eligible_series_count: int
    total_bytes: int
    source_preserved: bool
    preview_token: str


@dataclass(frozen=True, slots=True)
class CleanLibraryImportResult:
    """New managed-copy import created from a completed reference import."""

    source_job_id: int
    job_id: int
    eligible_file_count: int
    eligible_series_count: int


@dataclass(frozen=True, slots=True)
class _AdoptionSnapshot:
    file_count: int
    series_count: int
    total_bytes: int
    digest: str


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_application_secret(), salt=_TOKEN_SALT)


async def _load_source_job(session: AsyncSession, source_job_id: int) -> ImportJob:
    job = await session.get(ImportJob, source_job_id, populate_existing=True)
    if job is None:
        raise NotFoundError("ImportJob", source_job_id)
    if job.status is not ImportJobStatus.COMPLETED:
        raise ValidationError("The source import must be complete before standardization.")
    if job.archived_at is not None:
        raise ValidationError("Restore the archived import before standardizing its library.")
    return job


async def _require_mixed_folder_repairs_complete(
    session: AsyncSession,
    source_job_id: int,
) -> None:
    repair_count, _file_count = await count_completed_import_cleanup_scope(
        session,
        source_job_id,
        CompletedImportCleanupAction.RESOLVE_MIXED_FOLDER_FILES,
    )
    if repair_count:
        raise ValidationError(
            "Resolve mixed-folder files before building the clean Pullbox library."
        )


def _eligible_sources(source_job_id: int) -> tuple[ColumnElement[bool], ...]:
    return (
        ImportedFile.import_job_id == source_job_id,
        ImportedFile.status == ImportedFileStatus.IMPORTED,
        ImportedFile.matched_issue_id == Issue.id,
        ImportedFile.library_file_id == LibraryFile.id,
        LibraryFile.issue_id == Issue.id,
        LibraryFile.storage_mode == LibraryFileStorageMode.REFERENCED,
        LibraryFile.file_path == ImportedFile.file_path,
    )


async def _build_snapshot(session: AsyncSession, source_job_id: int) -> _AdoptionSnapshot:
    digest = sha256()
    series_ids: set[int] = set()
    file_count = 0
    total_bytes = 0
    stream = await session.stream(
        select(
            ImportedFile.id,
            LibraryFile.id,
            Issue.id,
            Issue.series_id,
            LibraryFile.file_path,
            LibraryFile.file_size,
            LibraryFile.updated_at,
            ImportedFile.updated_at,
        )
        .select_from(ImportedFile)
        .join(LibraryFile, LibraryFile.id == ImportedFile.library_file_id)
        .join(Issue, Issue.id == LibraryFile.issue_id)
        .where(*_eligible_sources(source_job_id))
        .order_by(ImportedFile.id)
        .execution_options(yield_per=2_000)
    )
    async for row in stream:
        file_count += 1
        series_ids.add(int(row[3]))
        total_bytes += int(row[5])
        digest.update(
            (
                f"{int(row[0])}|{int(row[1])}|{int(row[2])}|{row[4]}|"
                f"{row[6].isoformat()}|{row[7].isoformat()}\n"
            ).encode()
        )
    return _AdoptionSnapshot(
        file_count=file_count,
        series_count=len(series_ids),
        total_bytes=total_bytes,
        digest=digest.hexdigest(),
    )


def _paths_overlap(first: str, second: str) -> bool:
    first_path = Path(first).resolve(strict=False)
    second_path = Path(second).resolve(strict=False)
    return (
        first_path == second_path
        or first_path.is_relative_to(second_path)
        or second_path.is_relative_to(first_path)
    )


async def _load_target_root(
    session: AsyncSession,
    target_root_id: int,
    source_job_id: int,
) -> LibraryRoot:
    root = await session.get(LibraryRoot, target_root_id)
    if root is None:
        raise NotFoundError("LibraryRoot", target_root_id)
    if not root.enabled or not root.allow_managed_writes:
        raise ValidationError("Choose an enabled library root that allows managed writes.")
    source_root_paths = set(
        (
            await session.scalars(
                select(LibraryRoot.path)
                .select_from(ImportedFile)
                .join(LibraryFile, LibraryFile.id == ImportedFile.library_file_id)
                .join(Issue, Issue.id == LibraryFile.issue_id)
                .join(LibraryRoot, LibraryRoot.id == LibraryFile.library_root_id)
                .where(*_eligible_sources(source_job_id))
                .distinct()
            )
        ).all()
    )
    if any(_paths_overlap(root.path, source_path) for source_path in source_root_paths):
        raise ValidationError(
            "Choose a separate managed library root that does not overlap the Mylar source."
        )
    return root


async def preview_clean_library_import(
    session: AsyncSession,
    source_job_id: int,
    *,
    target_root_id: int,
    actor_id: int,
) -> CleanLibraryImportPreview:
    """Preview referenced files that can become a clean managed library."""
    await _load_source_job(session, source_job_id)
    await _require_mixed_folder_repairs_complete(session, source_job_id)
    snapshot = await _build_snapshot(session, source_job_id)
    if snapshot.file_count == 0:
        raise ValidationError("This import has no referenced files available to standardize.")
    await _load_target_root(session, target_root_id, source_job_id)
    token = str(
        _serializer().dumps(
            {
                "version": _TOKEN_VERSION,
                "source_job_id": source_job_id,
                "target_root_id": target_root_id,
                "actor_id": actor_id,
                "snapshot": {
                    "file_count": snapshot.file_count,
                    "series_count": snapshot.series_count,
                    "total_bytes": snapshot.total_bytes,
                    "digest": snapshot.digest,
                },
            }
        )
    )
    return CleanLibraryImportPreview(
        source_job_id=source_job_id,
        target_root_id=target_root_id,
        eligible_file_count=snapshot.file_count,
        eligible_series_count=snapshot.series_count,
        total_bytes=snapshot.total_bytes,
        source_preserved=True,
        preview_token=token,
    )


def _snapshot_from_token(
    token: str,
    *,
    source_job_id: int,
    target_root_id: int,
    actor_id: int,
) -> _AdoptionSnapshot:
    try:
        payload = _serializer().loads(token, max_age=_TOKEN_MAX_AGE_SECONDS)
    except SignatureExpired as exc:
        raise ValidationError("The clean-library preview expired. Preview it again.") from exc
    except BadSignature as exc:
        raise ValidationError("The clean-library preview is invalid. Preview it again.") from exc
    if not isinstance(payload, Mapping):
        raise ValidationError("The clean-library preview is invalid. Preview it again.")
    if (
        payload.get("version") != _TOKEN_VERSION
        or payload.get("source_job_id") != source_job_id
        or payload.get("target_root_id") != target_root_id
        or payload.get("actor_id") != actor_id
    ):
        raise ValidationError("The clean-library scope changed. Preview it again.")
    raw_snapshot = payload.get("snapshot")
    if not isinstance(raw_snapshot, Mapping):
        raise ValidationError("The clean-library preview is invalid. Preview it again.")
    try:
        snapshot = _AdoptionSnapshot(
            file_count=int(raw_snapshot["file_count"]),
            series_count=int(raw_snapshot["series_count"]),
            total_bytes=int(raw_snapshot["total_bytes"]),
            digest=str(raw_snapshot["digest"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("The clean-library preview is invalid. Preview it again.") from exc
    if (
        snapshot.file_count <= 0
        or snapshot.series_count <= 0
        or snapshot.total_bytes < 0
        or not snapshot.digest
    ):
        raise ValidationError("The clean-library preview is invalid. Preview it again.")
    return snapshot


def _adoption_diagnostics(
    *,
    source_diagnostics: object,
    source_job_id: int,
    source_imported_file_id: int,
    source_library_file_id: int,
    source_path: str,
    source_library_root_id: int,
    source_signature: object,
) -> dict[str, object]:
    diagnostics = dict(source_diagnostics) if isinstance(source_diagnostics, dict) else {}
    diagnostics["library_adoption"] = {
        "schema_version": 1,
        "source_import_job_id": source_job_id,
        "source_imported_file_id": source_imported_file_id,
        "source_library_file_id": source_library_file_id,
        "source_path": source_path,
        "source_library_root_id": source_library_root_id,
        "source_signature": (dict(source_signature) if isinstance(source_signature, dict) else {}),
        "source_storage_mode": LibraryFileStorageMode.REFERENCED.value,
        "source_preserved": True,
    }
    return diagnostics


async def _create_adoption_series(
    session: AsyncSession,
    *,
    job: ImportJob,
    source_job_id: int,
    batch_callback: Callable[[int], Awaitable[None]] | None = None,
) -> dict[int, int]:
    imported_series_by_id: dict[int, int] = {}
    last_series_id = 0
    while True:
        rows = (
            await session.execute(
                select(
                    Series.id,
                    Series.title,
                    Series.year_start,
                    Series.comicvine_id,
                    func.count(ImportedFile.id),
                    func.min(LibraryFile.file_path),
                )
                .select_from(ImportedFile)
                .join(LibraryFile, LibraryFile.id == ImportedFile.library_file_id)
                .join(Issue, Issue.id == LibraryFile.issue_id)
                .join(Series, Series.id == Issue.series_id)
                .where(*_eligible_sources(source_job_id))
                .where(Series.id > last_series_id)
                .group_by(Series.id, Series.title, Series.year_start, Series.comicvine_id)
                .order_by(Series.id)
                .limit(_SERIES_BATCH_SIZE)
            )
        ).all()
        if not rows:
            break
        pending: list[ImportedSeries] = []
        for series_id, title, year_start, comicvine_id, file_count, sample_path in rows:
            imported_series = ImportedSeries(
                import_job_id=job.id,
                raw_series_name=title,
                raw_year=year_start,
                file_count=int(file_count),
                files_total=int(file_count),
                files_matched=int(file_count),
                sample_paths=[str(sample_path)] if sample_path else [],
                source_folder=str(Path(str(sample_path)).parent) if sample_path else None,
                has_files=True,
                cv_id=comicvine_id,
                cv_title=title,
                cv_year=year_start,
                cv_match_score=1.0,
                cv_match_method="clean_library_adoption",
                status=ImportSeriesStatus.DUPLICATE,
                selected_for_import=True,
                series_id=series_id,
                diagnostics={
                    "kind": "clean_library_adoption",
                    "source_import_job_id": source_job_id,
                    "source_preserved": True,
                },
            )
            session.add(imported_series)
            pending.append(imported_series)
        await session.flush()
        imported_series_by_id.update(
            {int(item.series_id): int(item.id) for item in pending if item.series_id is not None}
        )
        last_series_id = int(rows[-1][0])
        if batch_callback is not None:
            await batch_callback(len(imported_series_by_id))
    return imported_series_by_id


async def _create_adoption_files(
    session: AsyncSession,
    *,
    job_id: int,
    source_job_id: int,
    imported_series_by_id: dict[int, int],
    batch_callback: Callable[[int], Awaitable[None]] | None = None,
) -> None:
    completed = 0
    last_file_id = 0
    while True:
        rows = (
            (
                await session.execute(
                    select(
                        ImportedFile.id.label("source_imported_file_id"),
                        ImportedFile.diagnostics.label("source_diagnostics"),
                        LibraryFile.id.label("source_library_file_id"),
                        LibraryFile.file_path,
                        LibraryFile.file_name,
                        LibraryFile.file_size,
                        LibraryFile.file_format,
                        LibraryFile.has_comicinfo,
                        LibraryFile.source_signature,
                        LibraryFile.library_root_id.label("source_library_root_id"),
                        Issue.id.label("issue_id"),
                        Issue.comicvine_id.label("issue_comicvine_id"),
                        Issue.issue_number,
                        Issue.issue_number_text,
                        Series.id.label("series_id"),
                        Series.title.label("series_title"),
                        Series.year_start,
                    )
                    .select_from(ImportedFile)
                    .join(LibraryFile, LibraryFile.id == ImportedFile.library_file_id)
                    .join(Issue, Issue.id == LibraryFile.issue_id)
                    .join(Series, Series.id == Issue.series_id)
                    .where(*_eligible_sources(source_job_id))
                    .where(ImportedFile.id > last_file_id)
                    .order_by(ImportedFile.id)
                    .limit(_FILE_BATCH_SIZE)
                )
            )
            .mappings()
            .all()
        )
        if not rows:
            break
        pending: list[dict[str, object]] = []
        for row in rows:
            series_id = int(row["series_id"])
            issue_number = float(row["issue_number"])
            issue_number_text = row["issue_number_text"]
            pending.append(
                {
                    "import_job_id": job_id,
                    "import_series_id": imported_series_by_id[series_id],
                    "file_path": str(row["file_path"]),
                    "file_name": str(row["file_name"]),
                    "file_size": int(row["file_size"]),
                    "file_format": row["file_format"].value,
                    "parsed_series": str(row["series_title"]),
                    "parsed_issue_number": issue_number,
                    "parsed_year": row["year_start"],
                    "has_comicinfo": bool(row["has_comicinfo"]),
                    "comicvine_issue_id": row["issue_comicvine_id"],
                    "issue_number_raw": (
                        str(issue_number_text) if issue_number_text else f"{issue_number:g}"
                    ),
                    "status": ImportedFileStatus.CONFIRMED,
                    "matched_issue_id": int(row["issue_id"]),
                    "matched_issue_cv_id": row["issue_comicvine_id"],
                    "match_confidence": "high",
                    "match_method": "clean_library_adoption",
                    "include_in_import": True,
                    "source_signature": dict(row["source_signature"] or {}),
                    "diagnostics": _adoption_diagnostics(
                        source_diagnostics=row["source_diagnostics"],
                        source_job_id=source_job_id,
                        source_imported_file_id=int(row["source_imported_file_id"]),
                        source_library_file_id=int(row["source_library_file_id"]),
                        source_path=str(row["file_path"]),
                        source_library_root_id=int(row["source_library_root_id"]),
                        source_signature=row["source_signature"],
                    ),
                }
            )
        await session.execute(insert(ImportedFile), pending)
        completed += len(pending)
        last_file_id = int(rows[-1]["source_imported_file_id"])
        if batch_callback is not None:
            await batch_callback(completed)


def _snapshot_payload(snapshot: _AdoptionSnapshot) -> dict[str, object]:
    return {
        "file_count": snapshot.file_count,
        "series_count": snapshot.series_count,
        "total_bytes": snapshot.total_bytes,
        "digest": snapshot.digest,
    }


def _snapshot_from_job(job: ImportJob) -> _AdoptionSnapshot:
    raw = dict(job.progress_snapshot or {}).get("clean_library_source_snapshot")
    if not isinstance(raw, Mapping):
        raise ValidationError("The clean-library preparation scope is unavailable.")
    try:
        return _AdoptionSnapshot(
            file_count=int(raw["file_count"]),
            series_count=int(raw["series_count"]),
            total_bytes=int(raw["total_bytes"]),
            digest=str(raw["digest"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("The clean-library preparation scope is invalid.") from exc


async def _find_active_clean_library_import(
    session: AsyncSession,
    *,
    source_job_id: int,
    target_root_id: int,
) -> ImportJob | None:
    jobs = list(
        (
            await session.scalars(
                select(ImportJob)
                .where(
                    ImportJob.status.in_(
                        {
                            ImportJobStatus.IMPORTING,
                            ImportJobStatus.PAUSING,
                            ImportJobStatus.PAUSED,
                            ImportJobStatus.STALLED,
                            ImportJobStatus.CANCELLING,
                            ImportJobStatus.ROLLING_BACK,
                        }
                    ),
                    ImportJob.target_library_root_id == target_root_id,
                )
                .order_by(ImportJob.id.desc())
            )
        ).all()
    )
    for job in jobs:
        progress = dict(job.progress_snapshot or {})
        if (
            progress.get("clean_library_adoption") is True
            and int(progress.get("source_import_job_id") or 0) == source_job_id
        ):
            return job
    return None


async def prepare_clean_library_import(
    session: AsyncSession,
    job_id: int,
    *,
    progress_callback: Callable[[ImportProgressEvent], Awaitable[None]] | None = None,
) -> bool:
    """Materialize a clean-library plan inside the durable import worker."""
    job = await session.get(ImportJob, job_id, populate_existing=True)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    progress = dict(job.progress_snapshot or {})
    if progress.get("clean_library_adoption") is not True:
        return False
    if progress.get("clean_library_adoption_prepared") is True:
        return False

    source_job_id = int(progress.get("source_import_job_id") or 0)
    if source_job_id <= 0 or job.target_library_root_id is None:
        raise ValidationError("The clean-library preparation context is incomplete.")
    expected_snapshot = _snapshot_from_job(job)
    await _load_source_job(session, source_job_id)
    await _require_mixed_folder_repairs_complete(session, source_job_id)
    await _load_target_root(session, int(job.target_library_root_id), source_job_id)
    current_snapshot = await _build_snapshot(session, source_job_id)
    if current_snapshot != expected_snapshot:
        raise ValidationError(
            "The clean-library source changed after preview. Open the organizer and try again."
        )

    await session.execute(delete(ImportedFile).where(ImportedFile.import_job_id == job_id))
    await session.execute(delete(ImportedSeries).where(ImportedSeries.import_job_id == job_id))

    total_units = expected_snapshot.series_count + expected_snapshot.file_count

    async def report_progress(
        completed_units: int,
        *,
        message: str,
        item_label: str,
    ) -> None:
        await raise_if_job_cancelled(session, job_id)
        job.progress_snapshot = {
            **dict(job.progress_snapshot or {}),
            "clean_library_adoption": True,
            "clean_library_adoption_prepared": False,
            "source_import_job_id": source_job_id,
            "clean_library_source_snapshot": _snapshot_payload(expected_snapshot),
        }
        await emit_progress(
            session,
            job,
            ImportProgressEvent(
                job_id=job_id,
                status=ImportJobStatus.IMPORTING,
                mode="import",
                phase="clean_library_preparing",
                progress=phase_progress(
                    0,
                    _PREPARATION_PROGRESS_END,
                    completed_units,
                    total_units,
                ),
                message=message,
                current_file_name=item_label,
                current_file_stage="clean_library_preparing",
                current_file_progress_current=completed_units,
                current_file_progress_total=total_units,
                current_file_progress_pct=round((completed_units / total_units) * 100),
                current_file_progress_unit="items",
            ),
            progress_callback,
        )

    await report_progress(
        0,
        message="Preparing the clean-library work plan...",
        item_label="Preparing library records",
    )
    imported_series_by_id = await _create_adoption_series(
        session,
        job=job,
        source_job_id=source_job_id,
        batch_callback=lambda completed: report_progress(
            completed,
            message=(f"Prepared {completed:,} of {expected_snapshot.series_count:,} series."),
            item_label="Preparing series",
        ),
    )
    await _create_adoption_files(
        session,
        job_id=job_id,
        source_job_id=source_job_id,
        imported_series_by_id=imported_series_by_id,
        batch_callback=lambda completed: report_progress(
            expected_snapshot.series_count + completed,
            message=(f"Prepared {completed:,} of {expected_snapshot.file_count:,} files."),
            item_label="Preparing files",
        ),
    )

    job = await session.get(ImportJob, job_id, populate_existing=True)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    job.series_found = expected_snapshot.series_count
    job.series_duplicate = expected_snapshot.series_count
    job.total_files_found = expected_snapshot.file_count
    job.total_files_matched = expected_snapshot.file_count
    job.progress_snapshot = {
        **dict(job.progress_snapshot or {}),
        "clean_library_adoption": True,
        "clean_library_adoption_prepared": True,
        "source_import_job_id": source_job_id,
        "clean_library_source_snapshot": _snapshot_payload(expected_snapshot),
    }
    session.add(
        ImportJobLog(
            import_job_id=job.id,
            level="INFO",
            event="clean_library_adoption_prepared",
            message=(
                f"Prepared {expected_snapshot.file_count} referenced files for a clean managed "
                "library."
            ),
            data={
                "source_import_job_id": source_job_id,
                "target_library_root_id": job.target_library_root_id,
                "eligible_file_count": expected_snapshot.file_count,
                "eligible_series_count": expected_snapshot.series_count,
                "total_bytes": expected_snapshot.total_bytes,
                "source_preserved": True,
            },
        )
    )
    await report_progress(
        total_units,
        message="Clean-library plan ready. Starting file processing...",
        item_label="Library plan ready",
    )
    job.progress_snapshot = {
        **dict(job.progress_snapshot or {}),
        "clean_library_adoption_prepared": True,
    }
    await session.commit()
    return True


async def create_clean_library_import(
    session: AsyncSession,
    source_job_id: int,
    *,
    target_root_id: int,
    actor_id: int,
    preview_token: str,
) -> CleanLibraryImportResult:
    """Create a managed-copy import that adopts exact referenced library files."""
    source_job = await _load_source_job(session, source_job_id)
    await _require_mixed_folder_repairs_complete(session, source_job_id)
    target_root = await _load_target_root(session, target_root_id, source_job_id)
    snapshot = _snapshot_from_token(
        preview_token,
        source_job_id=source_job_id,
        target_root_id=target_root_id,
        actor_id=actor_id,
    )

    active_job = await _find_active_clean_library_import(
        session,
        source_job_id=source_job_id,
        target_root_id=target_root_id,
    )
    if active_job is not None:
        return CleanLibraryImportResult(
            source_job_id=source_job_id,
            job_id=int(active_job.id),
            eligible_file_count=snapshot.file_count,
            eligible_series_count=snapshot.series_count,
        )

    policy = await load_effective_library_ingest_policy(session, target_root)
    job = ImportJob(
        source_path=source_job.source_path,
        selected_file_paths=[],
        source_type=source_job.source_type,
        status=ImportJobStatus.IMPORTING,
        target_library_root_id=target_root.id,
        monitored=False,
        search_on_add=False,
        file_handling_mode=ImportFileHandlingMode.MANAGED_COPY,
        source_layout_snapshot=dict(source_job.source_layout_snapshot or {}),
        mylar3_path_map=dict(source_job.mylar3_path_map or {}),
        mylar3_path_map_confirmed=source_job.mylar3_path_map_confirmed,
        progress_snapshot={
            "mode": "import",
            "phase": "queued",
            "progress": 0,
            "message": "Preparing a clean Pullbox-managed library.",
            "source_import_job_id": source_job.id,
            "clean_library_adoption": True,
            "clean_library_adoption_prepared": False,
            "clean_library_source_snapshot": _snapshot_payload(snapshot),
        },
    )
    apply_ingest_policy_to_import_job(job, policy)
    session.add(job)
    await session.flush()

    job.series_found = snapshot.series_count
    job.series_duplicate = snapshot.series_count
    job.total_files_found = snapshot.file_count
    job.total_files_matched = snapshot.file_count
    session.add(
        ImportJobLog(
            import_job_id=job.id,
            level="INFO",
            event="clean_library_adoption_queued",
            message=(f"Queued {snapshot.file_count} referenced files for a clean managed library."),
            data={
                "source_import_job_id": source_job.id,
                "target_library_root_id": target_root.id,
                "eligible_file_count": snapshot.file_count,
                "eligible_series_count": snapshot.series_count,
                "total_bytes": snapshot.total_bytes,
                "source_preserved": True,
            },
        )
    )
    await session.flush()
    return CleanLibraryImportResult(
        source_job_id=source_job.id,
        job_id=job.id,
        eligible_file_count=snapshot.file_count,
        eligible_series_count=snapshot.series_count,
    )
