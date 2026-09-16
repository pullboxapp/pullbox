"""Explicit source cleanup for one-page import artifacts."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func, select

from pullbox.config import get_settings
from pullbox.core.config_resolver import get_application_secret
from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.core.library_file_ownership import (
    build_file_identity_signature,
    validate_file_identity_signature,
)
from pullbox.models.audit_log import AuditEventType
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.library import LibraryRoot
from pullbox.services.audit_service import AuditService
from pullbox.services.import_counters import recompute_file_counters, recompute_series_counters
from pullbox.services.import_review_actions import apply_safety_skip_to_file
from pullbox.services.import_runtime_settings import load_import_utility_trash_folder
from pullbox.services.import_safety_diagnostics import ImportSafetyCategory
from pullbox.services.import_story_arc_resolution import (
    refresh_story_arc_entries_for_import_files,
)
from pullbox.utilities.settings import (
    move_file_to_utility_trash,
    resolve_trash_directory,
    restore_file_from_utility_trash,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_TOKEN_SALT: Final = "import-one-page-source-cleanup-v1"
_TOKEN_MAX_AGE_SECONDS: Final = 15 * 60


@dataclass(frozen=True, slots=True)
class OnePageSourceCleanupPreview:
    job_id: int
    file_id: int
    file_name: str
    can_move_to_trash: bool
    unavailable_reason: str
    preview_token: str | None


@dataclass(frozen=True, slots=True)
class OnePageSourceCleanupResult:
    trash_path: Path


@dataclass(frozen=True, slots=True)
class _CleanupContext:
    job: ImportJob
    imported_file: ImportedFile
    imported_series: ImportedSeries
    source: Path
    signature: dict[str, int | str]
    trash_dir: Path | None
    unavailable_reason: str


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_application_secret(), salt=_TOKEN_SALT)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


async def _load_context(
    session: AsyncSession,
    job_id: int,
    file_id: int,
) -> _CleanupContext:
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if job.status not in {ImportJobStatus.REVIEW, ImportJobStatus.COMPLETED}:
        raise ValidationError(
            "Source cleanup is available during review or after a completed import."
        )
    imported_file = await session.get(ImportedFile, file_id)
    if imported_file is None or imported_file.import_job_id != job_id:
        raise NotFoundError("ImportedFile", file_id)
    block = imported_file.diagnostics.get("safety_block", {})
    if (
        imported_file.status is not ImportedFileStatus.SAFETY_BLOCKED
        or not isinstance(block, Mapping)
        or block.get("category") != ImportSafetyCategory.SINGLE_PAGE_COMIC.value
    ):
        raise ValidationError("Only a one-page archive in safety review can use this action.")
    imported_series = await session.get(ImportedSeries, imported_file.import_series_id)
    if imported_series is None or imported_series.import_job_id != job_id:
        raise NotFoundError("ImportedSeries", imported_file.import_series_id)

    raw_source = Path(imported_file.file_path).expanduser()
    try:
        if raw_source.is_symlink():
            raise ValidationError("Symlinked source files cannot be removed from import review.")
        source = raw_source.resolve(strict=True)
        signature = build_file_identity_signature(source)
        validate_file_identity_signature(dict(imported_file.source_signature or {}), signature)
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError(
            "The source file changed or is unavailable. Re-scan before removing it."
        ) from exc

    unavailable_reason = ""
    if job.source_type is ImportSourceType.MYLAR3:
        roots = list(
            (
                await session.scalars(
                    select(LibraryRoot).where(
                        LibraryRoot.enabled.is_(True),
                        LibraryRoot.allow_referenced_registrations.is_(True),
                    )
                )
            ).all()
        )
        containing = []
        for root in roots:
            try:
                resolved_root = Path(root.path).expanduser().resolve(strict=True)
            except (OSError, RuntimeError, ValueError):
                continue
            if _is_within(source, resolved_root):
                containing.append(root)
        if not containing or not any(root.allow_managed_writes for root in containing):
            unavailable_reason = (
                "This Mylar source is registered as reference-only. Enable managed writes for "
                "its library root only if you intentionally want Pullbox to move source files."
            )
    else:
        try:
            source_root = Path(job.source_path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            source_root = source.parent
        if not _is_within(source, source_root) or not os.access(source.parent, os.W_OK):
            unavailable_reason = "Pullbox does not have permission to move this source file."

    configured_trash = await load_import_utility_trash_folder(session)
    settings = get_settings()
    trash_dir = resolve_trash_directory(
        configured_trash,
        library_root=settings.library_root,
        data_dir=settings.data_dir,
    )
    if trash_dir is None:
        unavailable_reason = (
            unavailable_reason
            or "Configure the Trash folder in Media Management before removing source files."
        )
    return _CleanupContext(
        job=job,
        imported_file=imported_file,
        imported_series=imported_series,
        source=source,
        signature=signature,
        trash_dir=trash_dir,
        unavailable_reason=unavailable_reason,
    )


async def preview_one_page_source_cleanup(
    session: AsyncSession,
    job_id: int,
    file_id: int,
    *,
    actor_id: int,
) -> OnePageSourceCleanupPreview:
    """Preview an individual source move without changing the source or database."""
    context = await _load_context(session, job_id, file_id)
    token = None
    if not context.unavailable_reason:
        token = str(
            _serializer().dumps(
                {
                    "job_id": job_id,
                    "file_id": file_id,
                    "actor_id": actor_id,
                    "signature": context.signature,
                }
            )
        )
    return OnePageSourceCleanupPreview(
        job_id=job_id,
        file_id=file_id,
        file_name=context.imported_file.file_name,
        can_move_to_trash=token is not None,
        unavailable_reason=context.unavailable_reason,
        preview_token=token,
    )


def _load_token(token: str) -> Mapping[str, object]:
    try:
        payload = _serializer().loads(token, max_age=_TOKEN_MAX_AGE_SECONDS)
    except SignatureExpired as exc:
        raise ValidationError("The source cleanup preview expired. Preview it again.") from exc
    except BadSignature as exc:
        raise ValidationError("The source cleanup preview is invalid. Preview it again.") from exc
    if not isinstance(payload, Mapping):
        raise ValidationError("The source cleanup preview is invalid. Preview it again.")
    return payload


async def move_one_page_source_to_trash(
    session: AsyncSession,
    job_id: int,
    file_id: int,
    *,
    actor_id: int,
    preview_token: str,
    actor_username: str | None = None,
    source_ip: str | None = None,
) -> OnePageSourceCleanupResult:
    """Move one explicitly previewed source to Trash and mark it skipped."""
    payload = _load_token(preview_token)
    if (
        payload.get("job_id") != job_id
        or payload.get("file_id") != file_id
        or payload.get("actor_id") != actor_id
    ):
        raise ValidationError("The source cleanup preview does not match this file.")
    expected_signature = payload.get("signature")
    if not isinstance(expected_signature, Mapping):
        raise ValidationError("The source cleanup preview is invalid. Preview it again.")

    context = await _load_context(session, job_id, file_id)
    if context.unavailable_reason or context.trash_dir is None:
        raise ValidationError(context.unavailable_reason or "Trash is not configured.")
    validate_file_identity_signature(dict(expected_signature), context.signature)

    relative_path = Path("import-review") / str(job_id) / context.source.name
    trash_path = await asyncio.to_thread(
        move_file_to_utility_trash,
        context.source,
        context.trash_dir,
        relative_path=relative_path,
    )
    try:
        apply_safety_skip_to_file(context.imported_file)
        await refresh_story_arc_entries_for_import_files(
            session,
            import_job_id=job_id,
            import_file_ids=[context.imported_file.id],
        )
        context.imported_series.selected_for_import = False
        await recompute_file_counters(
            session,
            context.job,
            series_ids=[context.imported_series.id],
        )
        remaining_file_count = await session.scalar(
            select(func.count(ImportedFile.id)).where(
                ImportedFile.import_series_id == context.imported_series.id,
                ImportedFile.status != ImportedFileStatus.SKIPPED,
            )
        )
        if not remaining_file_count:
            context.imported_series.status = ImportSeriesStatus.SKIPPED
        await recompute_series_counters(session, context.job)
        await AuditService.log_event(
            session,
            AuditEventType.IMPORT_SAFETY_SOURCE_TRASH,
            source_ip=source_ip,
            user_id=actor_id,
            username=actor_username,
            detail="One reviewed import source moved to Trash.",
            metadata={"job_id": job_id, "file_id": file_id},
        )
        await session.commit()
    except Exception:
        await session.rollback()
        await asyncio.to_thread(restore_file_from_utility_trash, trash_path, context.source)
        raise
    return OnePageSourceCleanupResult(trash_path=trash_path)
