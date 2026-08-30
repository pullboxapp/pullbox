"""Bounded, job-scoped bulk review for structured import safety categories."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Final, Literal

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import case, false, func, select

from pullbox.core.config_resolver import get_application_secret
from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.models.audit_log import AuditEventType
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.services.audit_service import AuditService
from pullbox.services.import_review_actions import apply_safety_allow_once_to_file
from pullbox.services.import_safety_diagnostics import ImportSafetyCategory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

IMPORT_SAFETY_BULK_CONFIRMATION: Final = "ALLOW ONCE"
IMPORT_SAFETY_BULK_PAGE_SIZE: Final = 200
IMPORT_SAFETY_PREVIEW_EXAMPLE_LIMIT: Final = 3
IMPORT_SAFETY_SNAPSHOT_PAGE_SIZE: Final = 20_000

_PREVIEW_TOKEN_SALT: Final = "import-safety-category-preview-v1"
_PREVIEW_TOKEN_MAX_AGE_SECONDS: Final = 15 * 60
_PREVIEW_TOKEN_VERSION: Final = 1
_ALLOW_ONCE_ACTION: Final = "allow_once"
_BULK_OVERRIDEABLE_CATEGORIES: Final = frozenset({ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT})


@dataclass(frozen=True, slots=True)
class ImportSafetyBulkSnapshot:
    """Aggregate scope fingerprint that does not retain file names or paths."""

    matching_count: int
    eligible_count: int
    min_file_id: int | None
    max_file_id: int | None
    max_updated_at: str | None
    scope_digest: str


@dataclass(frozen=True, slots=True)
class ImportSafetyBulkPreview:
    """Sanitized preview for one structured safety category in one job."""

    job_id: int
    source_type: ImportSourceType
    category: ImportSafetyCategory
    matching_count: int
    affected_count: int
    skipped_count: int
    examples: tuple[str, ...]
    overrideable: bool
    preview_token: str | None


@dataclass(frozen=True, slots=True)
class ImportSafetyBulkResult:
    """Counts from one category-specific allow-once operation."""

    job_id: int
    source_type: ImportSourceType
    category: ImportSafetyCategory
    affected_count: int
    skipped_count: int
    pages_processed: int


class ImportSafetyBulkInterruptedError(Exception):
    """Raised after bounded pages commit when the job ceases to be actionable."""

    def __init__(self, result: ImportSafetyBulkResult, *, reason: str) -> None:
        self.result = result
        self.reason = reason
        super().__init__("The safety bulk action stopped because the import job changed state.")


def _category_expression() -> Any:
    return ImportedFile.diagnostics["safety_block"]["category"].as_string()


def _overrideable_expression() -> Any:
    return ImportedFile.diagnostics["safety_block"]["overrideable"].as_boolean()


def _category_filters(job_id: int, category: ImportSafetyCategory) -> tuple[Any, ...]:
    return (
        ImportedFile.import_job_id == job_id,
        ImportedFile.status == ImportedFileStatus.SAFETY_BLOCKED,
        _category_expression() == category.value,
    )


def _eligible_expression(category: ImportSafetyCategory) -> Any:
    if category is not ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT:
        return false()
    return _overrideable_expression().is_(True)


def _snapshot_updated_at(value: datetime | None) -> str | None:
    return value.isoformat(timespec="microseconds") if value is not None else None


async def _load_scope_digest(
    session: AsyncSession,
    job_id: int,
    category: ImportSafetyCategory,
    *,
    max_file_id: int | None,
    expected_count: int,
) -> str:
    """Hash exact scoped row identities in bounded keyset pages without retaining them."""
    digest = sha256()
    if max_file_id is None or expected_count == 0:
        return digest.hexdigest()
    cursor = 0
    hashed_count = 0
    while cursor < max_file_id:
        rows = (
            await session.execute(
                select(
                    ImportedFile.id,
                    ImportedFile.updated_at,
                    _overrideable_expression(),
                )
                .where(
                    *_category_filters(job_id, category),
                    ImportedFile.id > cursor,
                    ImportedFile.id <= max_file_id,
                )
                .order_by(ImportedFile.id)
                .limit(IMPORT_SAFETY_SNAPSHOT_PAGE_SIZE)
            )
        ).all()
        if not rows:
            break
        for file_id, updated_at, overrideable in rows:
            digest.update(
                (
                    f"{int(file_id)}|{_snapshot_updated_at(updated_at)}|"
                    f"{1 if overrideable is True else 0}\n"
                ).encode()
            )
            cursor = int(file_id)
            hashed_count += 1
        # A preview or confirmation may cover 200K rows. Observe persisted
        # control state at each 20K-scalar boundary without retaining the scope.
        await _load_review_job(session, job_id)
    if hashed_count != expected_count:
        raise ValidationError("The safety category changed while it was being previewed.")
    return digest.hexdigest()


async def _load_review_job(session: AsyncSession, job_id: int) -> ImportJob:
    job = await session.get(ImportJob, job_id, populate_existing=True)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if job.status != ImportJobStatus.REVIEW:
        raise ValidationError("Job must be in REVIEW state to update safety review files")
    if job.control_request != ImportControlRequest.NONE:
        raise ValidationError("The import job has a pending control request")
    return job


async def _load_snapshot(
    session: AsyncSession,
    job_id: int,
    category: ImportSafetyCategory,
) -> ImportSafetyBulkSnapshot:
    eligible = _eligible_expression(category)
    row = (
        await session.execute(
            select(
                func.count(ImportedFile.id),
                func.coalesce(func.sum(case((eligible, 1), else_=0)), 0),
                func.min(ImportedFile.id),
                func.max(ImportedFile.id),
                func.max(ImportedFile.updated_at),
            ).where(*_category_filters(job_id, category))
        )
    ).one()
    matching_count = int(row[0] or 0)
    max_file_id = int(row[3]) if row[3] is not None else None
    scope_digest = await _load_scope_digest(
        session,
        job_id,
        category,
        max_file_id=max_file_id,
        expected_count=matching_count,
    )
    return ImportSafetyBulkSnapshot(
        matching_count=matching_count,
        eligible_count=int(row[1] or 0),
        min_file_id=int(row[2]) if row[2] is not None else None,
        max_file_id=max_file_id,
        max_updated_at=_snapshot_updated_at(row[4]),
        scope_digest=scope_digest,
    )


def _safe_example_name(value: str) -> str:
    normalized = value.replace("\\", "/").rstrip("/")
    leaf = normalized.rsplit("/", maxsplit=1)[-1]
    safe_leaf = "".join(character for character in leaf if character >= " " and character != "\x7f")
    return (safe_leaf or "File")[:200]


async def _load_bounded_examples(
    session: AsyncSession,
    job_id: int,
    category: ImportSafetyCategory,
    *,
    limit: int,
) -> tuple[str, ...]:
    if limit <= 0:
        return ()
    names = (
        (
            await session.execute(
                select(ImportedFile.file_name)
                .where(*_category_filters(job_id, category))
                .order_by(ImportedFile.id)
                .limit(min(limit, IMPORT_SAFETY_PREVIEW_EXAMPLE_LIMIT))
            )
        )
        .scalars()
        .all()
    )
    return tuple(_safe_example_name(str(name)) for name in names)


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_application_secret(), salt=_PREVIEW_TOKEN_SALT)


def _snapshot_payload(snapshot: ImportSafetyBulkSnapshot) -> dict[str, object]:
    return {
        "matching_count": snapshot.matching_count,
        "eligible_count": snapshot.eligible_count,
        "min_file_id": snapshot.min_file_id,
        "max_file_id": snapshot.max_file_id,
        "max_updated_at": snapshot.max_updated_at,
        "scope_digest": snapshot.scope_digest,
    }


def _build_preview_token(
    *,
    job: ImportJob,
    category: ImportSafetyCategory,
    actor_id: int,
    snapshot: ImportSafetyBulkSnapshot,
) -> str:
    return str(
        _serializer().dumps(
            {
                "version": _PREVIEW_TOKEN_VERSION,
                "job_id": job.id,
                "source_type": job.source_type.value,
                "category": category.value,
                "action": _ALLOW_ONCE_ACTION,
                "actor_id": actor_id,
                "snapshot": _snapshot_payload(snapshot),
            }
        )
    )


def _load_preview_token(token: str) -> Mapping[str, object]:
    try:
        payload = _serializer().loads(token, max_age=_PREVIEW_TOKEN_MAX_AGE_SECONDS)
    except SignatureExpired as exc:
        raise ValidationError("The safety preview expired. Preview the category again.") from exc
    except BadSignature as exc:
        raise ValidationError("The safety preview is invalid. Preview the category again.") from exc
    if not isinstance(payload, Mapping):
        raise ValidationError("The safety preview is invalid. Preview the category again.")
    return payload


def _token_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("The safety preview is invalid. Preview the category again.")
    return value


def _token_snapshot(payload: Mapping[str, object]) -> ImportSafetyBulkSnapshot:
    raw_snapshot = payload.get("snapshot")
    if not isinstance(raw_snapshot, Mapping):
        raise ValidationError("The safety preview is invalid. Preview the category again.")

    matching_count = _token_int(raw_snapshot, "matching_count")
    eligible_count = _token_int(raw_snapshot, "eligible_count")
    raw_min_id = raw_snapshot.get("min_file_id")
    raw_max_id = raw_snapshot.get("max_file_id")
    if raw_min_id is not None and (isinstance(raw_min_id, bool) or not isinstance(raw_min_id, int)):
        raise ValidationError("The safety preview is invalid. Preview the category again.")
    if raw_max_id is not None and (isinstance(raw_max_id, bool) or not isinstance(raw_max_id, int)):
        raise ValidationError("The safety preview is invalid. Preview the category again.")
    raw_updated_at = raw_snapshot.get("max_updated_at")
    if raw_updated_at is not None and not isinstance(raw_updated_at, str):
        raise ValidationError("The safety preview is invalid. Preview the category again.")
    raw_scope_digest = raw_snapshot.get("scope_digest")
    if (
        not isinstance(raw_scope_digest, str)
        or len(raw_scope_digest) != 64
        or any(character not in "0123456789abcdef" for character in raw_scope_digest)
    ):
        raise ValidationError("The safety preview is invalid. Preview the category again.")
    return ImportSafetyBulkSnapshot(
        matching_count=matching_count,
        eligible_count=eligible_count,
        min_file_id=raw_min_id,
        max_file_id=raw_max_id,
        max_updated_at=raw_updated_at,
        scope_digest=raw_scope_digest,
    )


def _validate_preview_token_scope(
    payload: Mapping[str, object],
    *,
    job: ImportJob,
    category: ImportSafetyCategory,
    actor_id: int,
) -> ImportSafetyBulkSnapshot:
    if (
        payload.get("version") != _PREVIEW_TOKEN_VERSION
        or _token_int(payload, "job_id") != job.id
        or payload.get("source_type") != job.source_type.value
        or payload.get("category") != category.value
        or payload.get("action") != _ALLOW_ONCE_ACTION
        or _token_int(payload, "actor_id") != actor_id
    ):
        raise ValidationError("The safety preview does not match this job and category.")
    return _token_snapshot(payload)


async def preview_import_safety_category(
    session: AsyncSession,
    job_id: int,
    category: ImportSafetyCategory,
    *,
    actor_id: int,
    example_limit: int = IMPORT_SAFETY_PREVIEW_EXAMPLE_LIMIT,
) -> ImportSafetyBulkPreview:
    """Preview one existing structured safety category without mutating rows."""
    job = await _load_review_job(session, job_id)
    snapshot = await _load_snapshot(session, job_id, category)
    if snapshot.matching_count == 0:
        raise ValidationError(
            "No safety-blocked files in this job use the selected structured category."
        )

    can_override = category in _BULK_OVERRIDEABLE_CATEGORIES and snapshot.eligible_count > 0
    examples = await _load_bounded_examples(
        session,
        job_id,
        category,
        limit=example_limit,
    )
    preview_token = (
        _build_preview_token(
            job=job,
            category=category,
            actor_id=actor_id,
            snapshot=snapshot,
        )
        if can_override
        else None
    )
    return ImportSafetyBulkPreview(
        job_id=job.id,
        source_type=job.source_type,
        category=category,
        matching_count=snapshot.matching_count,
        affected_count=snapshot.eligible_count if can_override else 0,
        skipped_count=(
            snapshot.matching_count - snapshot.eligible_count
            if can_override
            else snapshot.matching_count
        ),
        examples=examples,
        overrideable=can_override,
        preview_token=preview_token,
    )


def _result(
    *,
    job_id: int,
    source_type: ImportSourceType,
    category: ImportSafetyCategory,
    affected_count: int,
    original_matching_count: int,
    pages_processed: int,
) -> ImportSafetyBulkResult:
    return ImportSafetyBulkResult(
        job_id=job_id,
        source_type=source_type,
        category=category,
        affected_count=affected_count,
        skipped_count=max(original_matching_count - affected_count, 0),
        pages_processed=pages_processed,
    )


async def _write_durable_bulk_audit(
    session: AsyncSession,
    result: ImportSafetyBulkResult,
    *,
    actor_id: int,
    actor_username: str | None,
    source_ip: str | None,
    outcome: Literal["requested", "completed", "interrupted"],
) -> None:
    """Commit a fixed-field, path-free audit record before returning."""
    detail_by_outcome = {
        "requested": "Import safety category override requested.",
        "completed": "Import safety category override completed.",
        "interrupted": "Import safety category override interrupted.",
    }
    await AuditService.log_event(
        session,
        AuditEventType.IMPORT_SAFETY_BULK_OVERRIDE,
        source_ip=source_ip,
        user_id=actor_id,
        username=actor_username,
        detail=detail_by_outcome[outcome],
        metadata={
            "job_id": result.job_id,
            "source_type": result.source_type.value,
            "category": result.category.value,
            "action": _ALLOW_ONCE_ACTION,
            "affected_count": result.affected_count,
            "skipped_count": result.skipped_count,
            "outcome": outcome,
        },
    )
    await session.commit()


async def allow_import_safety_category_once(
    session: AsyncSession,
    job_id: int,
    category: ImportSafetyCategory,
    *,
    actor_id: int,
    actor_username: str | None = None,
    source_ip: str | None = None,
    preview_token: str,
    page_size: int = IMPORT_SAFETY_BULK_PAGE_SIZE,
) -> ImportSafetyBulkResult:
    """Apply one previewed size-limit exception in bounded committed pages."""
    if category not in _BULK_OVERRIDEABLE_CATEGORIES:
        raise ValidationError("This safety category cannot be bulk-overridden.")
    if page_size < 1 or page_size > IMPORT_SAFETY_BULK_PAGE_SIZE:
        raise ValidationError(
            f"Safety bulk page size must be between 1 and {IMPORT_SAFETY_BULK_PAGE_SIZE}."
        )

    job = await _load_review_job(session, job_id)
    payload = _load_preview_token(preview_token)
    preview_snapshot = _validate_preview_token_scope(
        payload,
        job=job,
        category=category,
        actor_id=actor_id,
    )
    if preview_snapshot.eligible_count <= 0 or preview_snapshot.max_file_id is None:
        raise ValidationError("This safety category has no eligible files to override.")

    requested_result = _result(
        job_id=job_id,
        source_type=job.source_type,
        category=category,
        affected_count=preview_snapshot.eligible_count,
        original_matching_count=preview_snapshot.matching_count,
        pages_processed=0,
    )
    await _write_durable_bulk_audit(
        session,
        requested_result,
        actor_id=actor_id,
        actor_username=actor_username,
        source_ip=source_ip,
        outcome="requested",
    )

    # The requested audit commit releases the read transaction. Revalidate the
    # exact row-identity digest before the first mutable page so a concurrent
    # category or eligibility change cannot reuse the signed preview.
    try:
        job = await _load_review_job(session, job_id)
        post_audit_snapshot = await _load_snapshot(session, job_id, category)
    except (NotFoundError, ValidationError):
        interrupted_result = _result(
            job_id=job_id,
            source_type=job.source_type,
            category=category,
            affected_count=0,
            original_matching_count=preview_snapshot.matching_count,
            pages_processed=0,
        )
        await _write_durable_bulk_audit(
            session,
            interrupted_result,
            actor_id=actor_id,
            actor_username=actor_username,
            source_ip=source_ip,
            outcome="interrupted",
        )
        raise
    if post_audit_snapshot != preview_snapshot:
        interrupted_result = _result(
            job_id=job_id,
            source_type=job.source_type,
            category=category,
            affected_count=0,
            original_matching_count=preview_snapshot.matching_count,
            pages_processed=0,
        )
        await _write_durable_bulk_audit(
            session,
            interrupted_result,
            actor_id=actor_id,
            actor_username=actor_username,
            source_ip=source_ip,
            outcome="interrupted",
        )
        raise ValidationError("The safety category changed. Preview it again before confirming.")

    affected_count = 0
    pages_processed = 0
    cursor = 0
    allowed_at = datetime.now(UTC)
    max_file_id = preview_snapshot.max_file_id
    max_updated_at = (
        datetime.fromisoformat(preview_snapshot.max_updated_at)
        if preview_snapshot.max_updated_at is not None
        else None
    )

    while True:
        current_job = await session.get(ImportJob, job_id, populate_existing=True)
        if (
            current_job is None
            or current_job.status != ImportJobStatus.REVIEW
            or current_job.control_request != ImportControlRequest.NONE
        ):
            partial_result = _result(
                job_id=job_id,
                source_type=job.source_type,
                category=category,
                affected_count=affected_count,
                original_matching_count=preview_snapshot.matching_count,
                pages_processed=pages_processed,
            )
            await _write_durable_bulk_audit(
                session,
                partial_result,
                actor_id=actor_id,
                actor_username=actor_username,
                source_ip=source_ip,
                outcome="interrupted",
            )
            if affected_count > 0:
                raise ImportSafetyBulkInterruptedError(partial_result, reason="job_state_changed")
            raise ValidationError("The import job is no longer available for safety review.")

        page = list(
            (
                await session.execute(
                    select(ImportedFile)
                    .where(
                        *_category_filters(job_id, category),
                        _eligible_expression(category),
                        ImportedFile.id > cursor,
                        ImportedFile.id <= max_file_id,
                        *(
                            (ImportedFile.updated_at <= max_updated_at,)
                            if max_updated_at is not None
                            else ()
                        ),
                    )
                    .order_by(ImportedFile.id)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        if not page:
            break

        affected_series_ids: set[int] = set()
        for imp_file in page:
            cursor = max(cursor, imp_file.id)
            diagnostics = imp_file.diagnostics or {}
            raw_block = diagnostics.get("safety_block")
            if not isinstance(raw_block, Mapping):
                continue
            if (
                raw_block.get("category") != category.value
                or raw_block.get("overrideable") is not True
            ):
                continue
            apply_safety_allow_once_to_file(imp_file, allowed_at=allowed_at)
            affected_series_ids.add(imp_file.import_series_id)
            affected_count += 1

        if affected_series_ids:
            imported_series = list(
                (
                    await session.execute(
                        select(ImportedSeries).where(
                            ImportedSeries.import_job_id == job_id,
                            ImportedSeries.id.in_(sorted(affected_series_ids)),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for series in imported_series:
                series.selected_for_import = False
                if series.status in {ImportSeriesStatus.MATCHED, ImportSeriesStatus.DUPLICATE}:
                    diagnostics = dict(series.diagnostics or {})
                    diagnostics["rematch_pending"] = True
                    series.diagnostics = diagnostics

        await session.flush()
        await session.commit()
        pages_processed += 1
        session.sync_session.expunge_all()

    result = _result(
        job_id=job_id,
        source_type=job.source_type,
        category=category,
        affected_count=affected_count,
        original_matching_count=preview_snapshot.matching_count,
        pages_processed=pages_processed,
    )
    if affected_count != preview_snapshot.eligible_count:
        await _write_durable_bulk_audit(
            session,
            result,
            actor_id=actor_id,
            actor_username=actor_username,
            source_ip=source_ip,
            outcome="interrupted",
        )
        if affected_count > 0:
            raise ImportSafetyBulkInterruptedError(result, reason="scope_changed_during_apply")
        raise ValidationError("The safety category changed. Preview it again before confirming.")
    await _write_durable_bulk_audit(
        session,
        result,
        actor_id=actor_id,
        actor_username=actor_username,
        source_ip=source_ip,
        outcome="completed",
    )
    return result
