"""Safe, previewed recovery actions for completed collection imports."""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Final

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import case, exists, func, or_, select, update
from sqlalchemy.orm import aliased

from pullbox.core.config_resolver import get_application_secret
from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.models.audit_log import AuditEventType
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobLog,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.models.library import LibraryFile
from pullbox.services.audit_service import AuditService
from pullbox.services.import_counters import recompute_file_counters, recompute_series_counters
from pullbox.services.import_review_actions import apply_safety_allow_once_to_file
from pullbox.services.import_safety_diagnostics import ImportSafetyCategory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_PREVIEW_TOKEN_SALT: Final = "completed-import-cleanup-v1"
_PREVIEW_TOKEN_MAX_AGE_SECONDS: Final = 15 * 60
_PREVIEW_TOKEN_VERSION: Final = 1
_PAGE_SIZE: Final = 500
_EXAMPLE_LIMIT: Final = 3


class CompletedImportCleanupAction(enum.StrEnum):
    """Supported completed-import cleanup operations."""

    DISMISS_MISSING_REFERENCES = "dismiss_missing_references"
    SKIP_PROBABLE_COVERS = "skip_probable_covers"
    SKIP_UNUSABLE_FILES = "skip_unusable_files"
    ALLOW_OVERSIZED_FILES = "allow_oversized_files"
    RETRY_SOURCE_INSPECTION = "retry_source_inspection"
    NORMALIZE_ALREADY_OWNED = "normalize_already_owned"
    ACCEPT_RECOMMENDED_CONFLICTS = "accept_recommended_conflicts"


@dataclass(frozen=True, slots=True)
class CompletedImportCleanupSnapshot:
    """Exact identity summary for one previewed cleanup scope."""

    affected_count: int
    affected_file_count: int
    min_file_id: int | None
    max_file_id: int | None
    max_updated_at: str | None
    scope_digest: str


@dataclass(frozen=True, slots=True)
class CompletedImportCleanupPreview:
    """User-facing bounded preview of a completed-import cleanup action."""

    job_id: int
    action: CompletedImportCleanupAction
    affected_count: int
    affected_file_count: int
    item_unit: str
    examples: tuple[str, ...]
    preview_token: str


@dataclass(frozen=True, slots=True)
class CompletedImportCleanupResult:
    """Outcome of a completed-import cleanup action."""

    job_id: int
    action: CompletedImportCleanupAction
    affected_count: int
    affected_file_count: int
    requires_import_retry: bool
    retry_file_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class CompletedImportCleanupFilePage:
    """One bounded page of files in an actionable recovery scope."""

    items: tuple[ImportedFile, ...]
    total: int
    page: int
    page_size: int
    total_pages: int


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_application_secret(), salt=_PREVIEW_TOKEN_SALT)


def _category_expression() -> Any:
    return ImportedFile.diagnostics["safety_block"]["category"].as_string()


def _overrideable_expression() -> Any:
    return ImportedFile.diagnostics["safety_block"]["overrideable"].as_boolean()


def _safety_filter(*categories: ImportSafetyCategory) -> Any:
    return _category_expression().in_([category.value for category in categories])


def _candidate_conflict_groups(job_id: int) -> Any:
    """Groups with exactly one high-confidence preferred candidate."""
    candidate = aliased(ImportedFile)
    return (
        select(candidate.conflict_group_id)
        .where(
            candidate.import_job_id == job_id,
            candidate.status == ImportedFileStatus.CONFLICT,
            candidate.conflict_group_id.is_not(None),
            ~exists().where(LibraryFile.issue_id == candidate.matched_issue_id),
        )
        .group_by(candidate.conflict_group_id)
        .having(func.sum(case((candidate.is_preferred.is_(True), 1), else_=0)) == 1)
        .having(
            func.sum(
                case(
                    (
                        candidate.is_preferred.is_(True) & (candidate.match_confidence == "high"),
                        1,
                    ),
                    else_=0,
                )
            )
            == 1
        )
    )


def _fully_recoverable_conflict_series(job_id: int) -> Any:
    """Series whose remaining conflicts are all safe recommended groups."""
    conflict = aliased(ImportedFile)
    candidate_groups = _candidate_conflict_groups(job_id)
    return (
        select(conflict.import_series_id)
        .where(
            conflict.import_job_id == job_id,
            conflict.status == ImportedFileStatus.CONFLICT,
        )
        .group_by(conflict.import_series_id)
        .having(
            func.sum(
                case(
                    (
                        or_(
                            conflict.conflict_group_id.is_(None),
                            ~conflict.conflict_group_id.in_(candidate_groups),
                        ),
                        1,
                    ),
                    else_=0,
                )
            )
            == 0
        )
    )


def _eligible_conflict_groups(job_id: int) -> Any:
    """Recommended groups that can be resumed without stranding sibling conflicts."""
    candidate = aliased(ImportedFile)
    return (
        select(candidate.conflict_group_id)
        .where(
            candidate.import_job_id == job_id,
            candidate.status == ImportedFileStatus.CONFLICT,
            candidate.conflict_group_id.in_(_candidate_conflict_groups(job_id)),
            candidate.import_series_id.in_(_fully_recoverable_conflict_series(job_id)),
        )
        .distinct()
    )


def _file_filters(job_id: int, action: CompletedImportCleanupAction) -> tuple[Any, ...]:
    filters: list[Any] = [ImportedFile.import_job_id == job_id]
    if action is CompletedImportCleanupAction.DISMISS_MISSING_REFERENCES:
        filters.extend(
            [
                ImportedFile.status == ImportedFileStatus.SAFETY_BLOCKED,
                _safety_filter(ImportSafetyCategory.SOURCE_MISSING),
            ]
        )
    elif action is CompletedImportCleanupAction.SKIP_PROBABLE_COVERS:
        filters.extend(
            [
                ImportedFile.status == ImportedFileStatus.SAFETY_BLOCKED,
                _safety_filter(ImportSafetyCategory.SINGLE_PAGE_COMIC),
            ]
        )
    elif action is CompletedImportCleanupAction.SKIP_UNUSABLE_FILES:
        filters.extend(
            [
                ImportedFile.status == ImportedFileStatus.SAFETY_BLOCKED,
                _safety_filter(
                    ImportSafetyCategory.ZERO_BYTE,
                    ImportSafetyCategory.ARCHIVE_NO_PAGES,
                    ImportSafetyCategory.UNSUPPORTED_FILE_TYPE,
                ),
            ]
        )
    elif action is CompletedImportCleanupAction.ALLOW_OVERSIZED_FILES:
        filters.extend(
            [
                ImportedFile.status == ImportedFileStatus.SAFETY_BLOCKED,
                _safety_filter(ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT),
                _overrideable_expression().is_(True),
            ]
        )
    elif action is CompletedImportCleanupAction.RETRY_SOURCE_INSPECTION:
        filters.extend(
            [
                ImportedFile.status == ImportedFileStatus.SAFETY_BLOCKED,
                _safety_filter(
                    ImportSafetyCategory.PERMISSION_UNREADABLE,
                    ImportSafetyCategory.ARCHIVE_INSPECTION_FAILED,
                    ImportSafetyCategory.SOURCE_CHANGED,
                ),
            ]
        )
    elif action is CompletedImportCleanupAction.NORMALIZE_ALREADY_OWNED:
        filters.extend(
            [
                ImportedFile.status == ImportedFileStatus.CONFLICT,
                ImportedFile.matched_issue_id.is_not(None),
                exists().where(LibraryFile.issue_id == ImportedFile.matched_issue_id),
            ]
        )
    elif action is CompletedImportCleanupAction.ACCEPT_RECOMMENDED_CONFLICTS:
        filters.extend(
            [
                ImportedFile.status == ImportedFileStatus.CONFLICT,
                ImportedFile.conflict_group_id.in_(_eligible_conflict_groups(job_id)),
            ]
        )
    else:  # pragma: no cover - exhaustive enum guard
        raise ValidationError("Unsupported completed-import cleanup action.")
    return tuple(filters)


async def _load_completed_job(session: AsyncSession, job_id: int) -> ImportJob:
    job = await session.get(ImportJob, job_id, populate_existing=True)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if job.status is not ImportJobStatus.COMPLETED:
        raise ValidationError("Job must be in COMPLETED state for recovery cleanup")
    if job.control_request is not ImportControlRequest.NONE:
        raise ValidationError("The import job has a pending control request")
    if job.archived_at is not None:
        raise ValidationError("Archived import jobs must be restored before cleanup")
    return job


def _safe_example_name(value: str) -> str:
    normalized = value.replace("\\", "/").rstrip("/")
    leaf = normalized.rsplit("/", maxsplit=1)[-1]
    safe_leaf = "".join(character for character in leaf if character >= " " and character != "\x7f")
    return (safe_leaf or "File")[:200]


async def _load_snapshot(
    session: AsyncSession,
    job_id: int,
    action: CompletedImportCleanupAction,
) -> CompletedImportCleanupSnapshot:
    filters = _file_filters(job_id, action)
    aggregate = (
        await session.execute(
            select(
                func.count(ImportedFile.id),
                func.min(ImportedFile.id),
                func.max(ImportedFile.id),
                func.max(ImportedFile.updated_at),
            ).where(*filters)
        )
    ).one()
    file_count = int(aggregate[0] or 0)
    if file_count == 0:
        return CompletedImportCleanupSnapshot(0, 0, None, None, None, sha256().hexdigest())

    digest = sha256()
    group_ids: set[int] = set()
    result = await session.stream(
        select(
            ImportedFile.id,
            ImportedFile.conflict_group_id,
            ImportedFile.updated_at,
        )
        .where(*filters)
        .order_by(ImportedFile.id)
        .execution_options(yield_per=20_000)
    )
    try:
        async for rows in result.partitions(20_000):
            for file_id, conflict_group_id, updated_at in rows:
                digest_line = (
                    f"{int(file_id)}|{int(conflict_group_id or 0)}|{updated_at.isoformat()}\n"
                )
                digest.update(digest_line.encode())
                if conflict_group_id is not None:
                    group_ids.add(int(conflict_group_id))
    finally:
        await result.close()
    affected_count = (
        len(group_ids)
        if action is CompletedImportCleanupAction.ACCEPT_RECOMMENDED_CONFLICTS
        else file_count
    )
    return CompletedImportCleanupSnapshot(
        affected_count=affected_count,
        affected_file_count=file_count,
        min_file_id=int(aggregate[1]),
        max_file_id=int(aggregate[2]),
        max_updated_at=aggregate[3].isoformat(timespec="microseconds"),
        scope_digest=digest.hexdigest(),
    )


async def count_completed_import_cleanup_scope(
    session: AsyncSession,
    job_id: int,
    action: CompletedImportCleanupAction,
) -> tuple[int, int]:
    """Return action and file counts without hashing the full preview scope."""
    filters = _file_filters(job_id, action)
    file_count = int(
        (await session.scalar(select(func.count(ImportedFile.id)).where(*filters))) or 0
    )
    if action is not CompletedImportCleanupAction.ACCEPT_RECOMMENDED_CONFLICTS:
        return file_count, file_count
    group_count = int(
        (
            await session.scalar(
                select(func.count(func.distinct(ImportedFile.conflict_group_id))).where(*filters)
            )
        )
        or 0
    )
    return group_count, file_count


async def list_completed_import_cleanup_files(
    session: AsyncSession,
    job_id: int,
    action: CompletedImportCleanupAction,
    *,
    page: int = 1,
    page_size: int = 25,
) -> CompletedImportCleanupFilePage:
    """Return a bounded, deterministic page for user review."""
    await _load_completed_job(session, job_id)
    normalized_page = max(1, int(page))
    normalized_page_size = min(max(1, int(page_size)), 100)
    filters = _file_filters(job_id, action)
    total = int((await session.scalar(select(func.count(ImportedFile.id)).where(*filters))) or 0)
    total_pages = max(1, (total + normalized_page_size - 1) // normalized_page_size)
    normalized_page = min(normalized_page, total_pages)
    items = tuple(
        (
            await session.scalars(
                select(ImportedFile)
                .where(*filters)
                .order_by(ImportedFile.id)
                .offset((normalized_page - 1) * normalized_page_size)
                .limit(normalized_page_size)
            )
        ).all()
    )
    return CompletedImportCleanupFilePage(
        items=items,
        total=total,
        page=normalized_page,
        page_size=normalized_page_size,
        total_pages=total_pages,
    )


async def list_completed_import_cleanup_examples(
    session: AsyncSession,
    job_id: int,
    action: CompletedImportCleanupAction,
    *,
    limit: int = _EXAMPLE_LIMIT,
) -> tuple[str, ...]:
    """Return sanitized example filenames without hydrating the full scope."""
    names = (
        await session.scalars(
            select(ImportedFile.file_name)
            .where(*_file_filters(job_id, action))
            .order_by(ImportedFile.id)
            .limit(min(max(1, int(limit)), 10))
        )
    ).all()
    return tuple(_safe_example_name(name) for name in names)


def _snapshot_payload(snapshot: CompletedImportCleanupSnapshot) -> dict[str, object]:
    return {
        "affected_count": snapshot.affected_count,
        "affected_file_count": snapshot.affected_file_count,
        "min_file_id": snapshot.min_file_id,
        "max_file_id": snapshot.max_file_id,
        "max_updated_at": snapshot.max_updated_at,
        "scope_digest": snapshot.scope_digest,
    }


def _load_token(token: str) -> Mapping[str, object]:
    try:
        payload = _serializer().loads(token, max_age=_PREVIEW_TOKEN_MAX_AGE_SECONDS)
    except SignatureExpired as exc:
        raise ValidationError("The cleanup preview expired. Preview the action again.") from exc
    except BadSignature as exc:
        raise ValidationError("The cleanup preview is invalid. Preview the action again.") from exc
    if not isinstance(payload, Mapping):
        raise ValidationError("The cleanup preview is invalid. Preview the action again.")
    return payload


def _snapshot_from_payload(payload: Mapping[str, object]) -> CompletedImportCleanupSnapshot:
    raw = payload.get("snapshot")
    if not isinstance(raw, Mapping):
        raise ValidationError("The cleanup preview is invalid. Preview the action again.")
    try:
        affected_count = int(raw["affected_count"])
        affected_file_count = int(raw["affected_file_count"])
        min_file_id = int(raw["min_file_id"]) if raw.get("min_file_id") is not None else None
        max_file_id = int(raw["max_file_id"]) if raw.get("max_file_id") is not None else None
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("The cleanup preview is invalid. Preview the action again.") from exc
    max_updated_at = raw.get("max_updated_at")
    scope_digest = raw.get("scope_digest")
    if not isinstance(max_updated_at, (str, type(None))) or not isinstance(scope_digest, str):
        raise ValidationError("The cleanup preview is invalid. Preview the action again.")
    return CompletedImportCleanupSnapshot(
        affected_count,
        affected_file_count,
        min_file_id,
        max_file_id,
        max_updated_at,
        scope_digest,
    )


async def preview_completed_import_cleanup(
    session: AsyncSession,
    job_id: int,
    action: CompletedImportCleanupAction,
    *,
    actor_id: int,
) -> CompletedImportCleanupPreview:
    """Return a bounded preview and actor-bound confirmation token."""
    job = await _load_completed_job(session, job_id)
    snapshot = await _load_snapshot(session, job_id, action)
    if snapshot.affected_count == 0:
        raise ValidationError("No files are eligible for this cleanup action.")
    examples = await list_completed_import_cleanup_examples(
        session,
        job_id,
        action,
    )
    token = str(
        _serializer().dumps(
            {
                "version": _PREVIEW_TOKEN_VERSION,
                "job_id": job.id,
                "action": action.value,
                "actor_id": actor_id,
                "snapshot": _snapshot_payload(snapshot),
            }
        )
    )
    return CompletedImportCleanupPreview(
        job_id=job.id,
        action=action,
        affected_count=snapshot.affected_count,
        affected_file_count=snapshot.affected_file_count,
        item_unit=(
            "group"
            if action is CompletedImportCleanupAction.ACCEPT_RECOMMENDED_CONFLICTS
            else "file"
        ),
        examples=examples,
        preview_token=token,
    )


def _validate_token(
    token: str,
    *,
    job_id: int,
    action: CompletedImportCleanupAction,
    actor_id: int,
) -> CompletedImportCleanupSnapshot:
    payload = _load_token(token)
    if (
        payload.get("version") != _PREVIEW_TOKEN_VERSION
        or payload.get("job_id") != job_id
        or payload.get("action") != action.value
        or payload.get("actor_id") != actor_id
    ):
        raise ValidationError("The cleanup preview does not match this job and action.")
    return _snapshot_from_payload(payload)


def _mark_skipped(file: ImportedFile, *, action: CompletedImportCleanupAction) -> None:
    diagnostics = dict(file.diagnostics or {})
    diagnostics["completed_import_cleanup"] = {
        "action": action.value,
        "resolved_at": datetime.now(UTC).isoformat(),
        "source_preserved": True,
    }
    file.status = ImportedFileStatus.SKIPPED
    file.include_in_import = False
    file.error_message = None
    file.match_method = "completed_import_cleanup"
    file.diagnostics = diagnostics


def _prepare_source_retry(file: ImportedFile) -> None:
    diagnostics = dict(file.diagnostics or {})
    raw_block = diagnostics.get("safety_block")
    if not isinstance(raw_block, Mapping):
        raise ValidationError("A selected source failure no longer has safety evidence.")
    diagnostics.pop("safety_block", None)
    diagnostics["source_revalidation"] = {
        **dict(raw_block),
        "kind": "source_revalidation",
        "retryable": True,
        "source": "completed_import_cleanup",
    }
    file.status = ImportedFileStatus.FAILED
    file.include_in_import = False
    file.diagnostics = diagnostics


async def _apply_file_action(
    session: AsyncSession,
    job: ImportJob,
    action: CompletedImportCleanupAction,
) -> tuple[set[int], tuple[int, ...], bool]:
    affected_series_ids: set[int] = set()
    affected_file_ids: list[int] = []
    requires_import_retry = False
    cursor = 0
    while True:
        files = list(
            (
                await session.scalars(
                    select(ImportedFile)
                    .where(*_file_filters(job.id, action), ImportedFile.id > cursor)
                    .order_by(ImportedFile.id)
                    .limit(_PAGE_SIZE)
                )
            ).all()
        )
        if not files:
            break
        cursor = int(files[-1].id)
        for file in files:
            affected_series_ids.add(int(file.import_series_id))
            affected_file_ids.append(int(file.id))
            if action in {
                CompletedImportCleanupAction.DISMISS_MISSING_REFERENCES,
                CompletedImportCleanupAction.SKIP_PROBABLE_COVERS,
                CompletedImportCleanupAction.SKIP_UNUSABLE_FILES,
            }:
                _mark_skipped(file, action=action)
            elif action is CompletedImportCleanupAction.ALLOW_OVERSIZED_FILES:
                apply_safety_allow_once_to_file(file, retry_import=True)
                requires_import_retry = True
            elif action is CompletedImportCleanupAction.RETRY_SOURCE_INSPECTION:
                _prepare_source_retry(file)
                requires_import_retry = True
            elif action is CompletedImportCleanupAction.NORMALIZE_ALREADY_OWNED:
                file.status = ImportedFileStatus.ALREADY_OWNED
                file.include_in_import = False
                file.error_message = None
            else:  # pragma: no cover - conflict groups use a separate path
                raise ValidationError("Unsupported file cleanup action.")
        await session.flush()
    return affected_series_ids, tuple(affected_file_ids), requires_import_retry


async def _apply_recommended_conflicts(
    session: AsyncSession,
    job: ImportJob,
) -> set[int]:
    eligible_group_ids = [
        int(group_id)
        for group_id in (
            await session.scalars(
                select(ImportedFile.conflict_group_id)
                .where(
                    ImportedFile.import_job_id == job.id,
                    ImportedFile.status == ImportedFileStatus.CONFLICT,
                    ImportedFile.conflict_group_id.in_(_eligible_conflict_groups(job.id)),
                )
                .distinct()
                .order_by(ImportedFile.conflict_group_id)
            )
        ).all()
        if group_id is not None
    ]
    if not eligible_group_ids:
        return set()
    affected_series_ids = set(
        await session.scalars(
            select(ImportedFile.import_series_id)
            .where(
                ImportedFile.import_job_id == job.id,
                ImportedFile.status == ImportedFileStatus.CONFLICT,
                ImportedFile.conflict_group_id.in_(eligible_group_ids),
            )
            .distinct()
        )
    )
    await session.execute(
        update(ImportedFile)
        .where(
            ImportedFile.import_job_id == job.id,
            ImportedFile.status == ImportedFileStatus.CONFLICT,
            ImportedFile.conflict_group_id.in_(eligible_group_ids),
            ImportedFile.is_preferred.is_(False),
        )
        .values(status=ImportedFileStatus.SKIPPED, include_in_import=False)
    )
    await session.execute(
        update(ImportedFile)
        .where(
            ImportedFile.import_job_id == job.id,
            ImportedFile.status == ImportedFileStatus.CONFLICT,
            ImportedFile.conflict_group_id.in_(eligible_group_ids),
            ImportedFile.is_preferred.is_(True),
            ImportedFile.match_confidence == "high",
        )
        .values(status=ImportedFileStatus.CONFIRMED, include_in_import=True)
    )
    await session.flush()
    return {int(series_id) for series_id in affected_series_ids}


async def _prepare_series_for_retry(
    session: AsyncSession,
    job: ImportJob,
    series_ids: set[int],
) -> bool:
    if not series_ids:
        return False
    remaining_conflicts = set(
        await session.scalars(
            select(ImportedFile.import_series_id)
            .where(
                ImportedFile.import_series_id.in_(series_ids),
                ImportedFile.status == ImportedFileStatus.CONFLICT,
            )
            .distinct()
        )
    )
    retry_series_ids = sorted(series_ids - {int(value) for value in remaining_conflicts})
    if not retry_series_ids:
        return False
    await session.execute(
        update(ImportedSeries)
        .where(ImportedSeries.id.in_(retry_series_ids))
        .values(
            status=ImportSeriesStatus.CONFIRMED,
            selected_for_import=True,
            error_message=None,
        )
    )
    job.status = ImportJobStatus.IMPORTING
    job.error_message = None
    return True


async def apply_completed_import_cleanup(
    session: AsyncSession,
    job_id: int,
    action: CompletedImportCleanupAction,
    *,
    actor_id: int,
    preview_token: str,
    actor_username: str | None = None,
    source_ip: str | None = None,
) -> CompletedImportCleanupResult:
    """Apply exactly the previewed cleanup scope without touching source files."""
    job = await _load_completed_job(session, job_id)
    preview_snapshot = _validate_token(
        preview_token,
        job_id=job_id,
        action=action,
        actor_id=actor_id,
    )
    current_snapshot = await _load_snapshot(session, job_id, action)
    if current_snapshot != preview_snapshot:
        raise ValidationError("The cleanup scope changed. Preview the action again.")

    if action is CompletedImportCleanupAction.ACCEPT_RECOMMENDED_CONFLICTS:
        affected_series_ids = await _apply_recommended_conflicts(session, job)
        affected_file_ids: tuple[int, ...] = ()
        requires_import_retry = await _prepare_series_for_retry(session, job, affected_series_ids)
    else:
        affected_series_ids, affected_file_ids, requires_import_retry = await _apply_file_action(
            session, job, action
        )
        if action is CompletedImportCleanupAction.ALLOW_OVERSIZED_FILES:
            requires_import_retry = await _prepare_series_for_retry(
                session, job, affected_series_ids
            )

    await recompute_file_counters(session, job, series_ids=sorted(affected_series_ids))
    await recompute_series_counters(session, job)
    result = CompletedImportCleanupResult(
        job_id=job.id,
        action=action,
        affected_count=preview_snapshot.affected_count,
        affected_file_count=preview_snapshot.affected_file_count,
        requires_import_retry=requires_import_retry,
        retry_file_ids=(
            affected_file_ids
            if action is CompletedImportCleanupAction.RETRY_SOURCE_INSPECTION
            else ()
        ),
    )
    item_unit = (
        "group" if action is CompletedImportCleanupAction.ACCEPT_RECOMMENDED_CONFLICTS else "file"
    )
    session.add(
        ImportJobLog(
            import_job_id=job.id,
            level="INFO",
            event="import_completed_cleanup_applied",
            message=(
                f"Applied {action.value} to {result.affected_count} "
                f"{item_unit}"
                f"{'s' if result.affected_count != 1 else ''}."
            ),
            data={
                "action": action.value,
                "affected_count": result.affected_count,
                "affected_file_count": result.affected_file_count,
                "requires_import_retry": result.requires_import_retry,
                "source_preserved": True,
            },
        )
    )
    await AuditService.log_event(
        session,
        AuditEventType.IMPORT_RECOVERY_BULK_ACTION,
        source_ip=source_ip,
        user_id=actor_id,
        username=actor_username,
        detail="Completed import recovery action applied.",
        metadata={
            "job_id": job.id,
            "action": action.value,
            "affected_count": result.affected_count,
            "affected_file_count": result.affected_file_count,
            "source_preserved": True,
        },
    )
    await session.flush()
    return result
