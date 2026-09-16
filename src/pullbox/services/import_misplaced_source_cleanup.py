"""Previewed source cleanup for exact cross-folder Mylar recoveries."""

from __future__ import annotations

import asyncio
import enum
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func, select

from pullbox.config import get_settings
from pullbox.core.config_resolver import get_application_secret
from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.core.filesystem_policy import is_invalid_path_text
from pullbox.core.library_file_ownership import (
    build_file_identity_signature,
    validate_file_identity_signature,
)
from pullbox.models.audit_log import AuditEventType
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportJob,
    ImportJobAction,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.models.library import LibraryFile, LibraryFileStorageMode, LibraryRoot
from pullbox.services.audit_service import AuditService
from pullbox.services.import_duplicate_copies import compute_content_hash
from pullbox.services.import_runtime_settings import load_import_utility_trash_folder
from pullbox.utilities.settings import (
    move_file_to_utility_trash,
    resolve_trash_directory,
    restore_file_from_utility_trash,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement


class MisplacedSourceCleanupAction(enum.StrEnum):
    """Explicit physical cleanup choices for one recovered Mylar source."""

    RESTORE_RECORDED_PATH = "restore_recorded_path"
    TRASH_IDENTICAL_DUPLICATE = "trash_identical_duplicate"


@dataclass(frozen=True, slots=True)
class MisplacedSourceCleanupPreview:
    """One actor-bound preview for a physical source change."""

    job_id: int
    file_id: int
    action: MisplacedSourceCleanupAction
    file_name: str
    source_path: str
    destination_path: str | None
    can_apply: bool
    unavailable_reason: str
    preview_token: str | None


@dataclass(frozen=True, slots=True)
class MisplacedSourceCleanupResult:
    """Result of one explicit source cleanup."""

    final_path: Path


@dataclass(frozen=True, slots=True)
class MisplacedSourceCleanupBulkPreview:
    """Signed preview for every currently eligible verified misplaced file."""

    job_id: int
    affected_count: int
    unavailable_count: int
    examples: tuple[str, ...]
    preview_token: str | None


@dataclass(frozen=True, slots=True)
class MisplacedSourceCleanupBulkResult:
    """Outcome of one verified bulk source-organization operation."""

    moved_count: int
    skipped_count: int


@dataclass(frozen=True, slots=True)
class MisplacedSourceCleanupFilePage:
    """One bounded page of exact source-cleanup candidates."""

    items: tuple[ImportedFile, ...]
    total: int
    page: int
    page_size: int
    total_pages: int


@dataclass(frozen=True, slots=True)
class _CleanupContext:
    job: ImportJob
    imported_file: ImportedFile
    library_file: LibraryFile
    source: Path
    destination: Path | None
    signature: dict[str, int | str]
    unavailable_reason: str


@dataclass(frozen=True, slots=True)
class _DuplicateCleanupContext:
    job: ImportJob
    imported_file: ImportedFile
    canonical_file: ImportedFile
    source: Path
    signature: dict[str, int | str]
    canonical_signature: dict[str, int | str]
    content_hash: str
    trash_dir: Path | None
    unavailable_reason: str


_TOKEN_SALT: Final = "import-misplaced-source-cleanup-v1"
_VERIFIED_CROSS_FOLDER_METHODS: Final = (
    "verified_cross_folder_issue_identity",
    "verified_cross_folder_series_issue_filename",
)
_TOKEN_MAX_AGE_SECONDS: Final = 15 * 60


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_application_secret(), salt=_TOKEN_SALT)


async def _load_completed_mylar_job(session: AsyncSession, job_id: int) -> ImportJob:
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if (
        job.status is not ImportJobStatus.COMPLETED
        or job.source_type is not ImportSourceType.MYLAR3
    ):
        raise ValidationError("Mylar source cleanup is available after a completed Mylar import.")
    return job


def _cleanup_scope_filters(
    job_id: int,
    action: MisplacedSourceCleanupAction,
) -> tuple[ColumnElement[bool], ...]:
    evidence = ImportedFile.diagnostics["mylar3_cross_folder_reconciliation"]
    base: tuple[ColumnElement[bool], ...] = (
        ImportedFile.import_job_id == job_id,
        evidence["method"].as_string().in_(_VERIFIED_CROSS_FOLDER_METHODS),
    )
    if action is MisplacedSourceCleanupAction.RESTORE_RECORDED_PATH:
        return (
            *base,
            ImportedFile.status == ImportedFileStatus.IMPORTED,
            evidence["role"].as_string() == "canonical",
            evidence["restored_at"].as_string().is_(None),
            ImportedFile.library_file_id.is_not(None),
        )
    if action is MisplacedSourceCleanupAction.TRASH_IDENTICAL_DUPLICATE:
        return (
            *base,
            ImportedFile.status == ImportedFileStatus.DUPLICATE_FILE,
            evidence["role"].as_string() == "identical_duplicate",
            ImportedFile.diagnostics["misplaced_source_cleanup"]["action"].as_string().is_(None),
            ImportedFile.duplicate_of_file_id.is_not(None),
            ImportedFile.content_hash.is_not(None),
        )
    raise ValidationError("This misplaced source cleanup action is not supported.")


async def count_misplaced_source_cleanup_files(
    session: AsyncSession,
    job_id: int,
    action: MisplacedSourceCleanupAction,
) -> int:
    """Count pending exact source-cleanup candidates without hydrating them."""
    await _load_completed_mylar_job(session, job_id)
    return int(
        (
            await session.scalar(
                select(func.count(ImportedFile.id)).where(*_cleanup_scope_filters(job_id, action))
            )
        )
        or 0
    )


async def list_misplaced_source_cleanup_files(
    session: AsyncSession,
    job_id: int,
    action: MisplacedSourceCleanupAction,
    *,
    page: int = 1,
    page_size: int = 25,
) -> MisplacedSourceCleanupFilePage:
    """Return a deterministic, bounded page of pending source cleanups."""
    await _load_completed_mylar_job(session, job_id)
    normalized_page = max(1, int(page))
    normalized_page_size = min(max(1, int(page_size)), 100)
    filters = _cleanup_scope_filters(job_id, action)
    total = int((await session.scalar(select(func.count(ImportedFile.id)).where(*filters))) or 0)
    total_pages = max(1, (total + normalized_page_size - 1) // normalized_page_size)
    normalized_page = min(normalized_page, total_pages)
    items = tuple(
        (
            await session.scalars(
                select(ImportedFile)
                .where(*filters)
                .order_by(ImportedFile.file_name, ImportedFile.id)
                .offset((normalized_page - 1) * normalized_page_size)
                .limit(normalized_page_size)
            )
        ).all()
    )
    return MisplacedSourceCleanupFilePage(
        items=items,
        total=total,
        page=normalized_page,
        page_size=normalized_page_size,
        total_pages=total_pages,
    )


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _safe_absolute_path(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value or is_invalid_path_text(value):
        raise ValidationError(f"The recorded {label} is invalid. Re-scan before cleanup.")
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise ValidationError(f"The recorded {label} is invalid. Re-scan before cleanup.")
    return path.absolute()


async def _reference_root_for_path(
    session: AsyncSession,
    *,
    lexical_path: Path,
    resolved_path: Path,
    require_managed_writes: bool,
) -> tuple[LibraryRoot | None, str]:
    roots = list(
        await session.scalars(
            select(LibraryRoot).where(
                LibraryRoot.enabled.is_(True),
                LibraryRoot.allow_referenced_registrations.is_(True),
            )
        )
    )
    matches: list[LibraryRoot] = []
    for root in roots:
        try:
            lexical_root = Path(root.path).expanduser().absolute()
            resolved_root = Path(root.path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            continue
        if _inside(lexical_path, lexical_root) and _inside(resolved_path, resolved_root):
            matches.append(root)
    if len(matches) != 1:
        return None, "The file does not belong to one unambiguous enabled library root."
    if require_managed_writes and not matches[0].allow_managed_writes:
        return None, (
            "This library root does not allow managed writes. Enable managed writes only when "
            "you are ready for Pullbox to change the Mylar source library."
        )
    return matches[0], ""


async def _load_restore_context(
    session: AsyncSession,
    job_id: int,
    file_id: int,
) -> _CleanupContext:
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if (
        job.status is not ImportJobStatus.COMPLETED
        or job.source_type is not ImportSourceType.MYLAR3
    ):
        raise ValidationError("Mylar source cleanup is available after a completed Mylar import.")
    imported_file = await session.get(ImportedFile, file_id)
    if imported_file is None or imported_file.import_job_id != job_id:
        raise NotFoundError("ImportedFile", file_id)
    evidence = dict(imported_file.diagnostics or {}).get("mylar3_cross_folder_reconciliation")
    if (
        imported_file.status is not ImportedFileStatus.IMPORTED
        or not isinstance(evidence, Mapping)
        or evidence.get("method") not in _VERIFIED_CROSS_FOLDER_METHODS
        or evidence.get("role") != "canonical"
    ):
        raise ValidationError(
            "Only an imported, exactly identified misplaced file can be restored."
        )
    if imported_file.library_file_id is None:
        raise ValidationError("The misplaced file no longer has a Pullbox library reference.")
    library_file = await session.get(LibraryFile, imported_file.library_file_id)
    if (
        library_file is None
        or library_file.storage_mode is not LibraryFileStorageMode.REFERENCED
        or library_file.issue_id != imported_file.matched_issue_id
    ):
        raise ValidationError("The misplaced file reference changed after import.")

    source_lexical = _safe_absolute_path(imported_file.file_path, label="source path")
    destination_lexical = _safe_absolute_path(evidence.get("recorded_path"), label="Mylar path")
    try:
        if source_lexical.is_symlink():
            raise ValidationError("Symlinked source files cannot use Mylar path restoration.")
        source = source_lexical.resolve(strict=True)
        destination = destination_lexical.resolve(strict=False)
        signature = build_file_identity_signature(source)
        validate_file_identity_signature(dict(imported_file.source_signature or {}), signature)
        validate_file_identity_signature(dict(library_file.source_signature or {}), signature)
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError(
            "The misplaced source changed or is unavailable. Re-scan before cleanup."
        ) from exc
    if Path(library_file.file_path).expanduser().resolve(strict=True) != source:
        raise ValidationError("The misplaced file reference changed after import.")
    if source == destination:
        return _CleanupContext(job, imported_file, library_file, source, None, signature, "")

    _source_root, source_reason = await _reference_root_for_path(
        session,
        lexical_path=source_lexical,
        resolved_path=source,
        require_managed_writes=False,
    )
    destination_parent = destination.parent
    try:
        resolved_parent = destination_parent.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        resolved_parent = destination_parent
    _destination_root, destination_reason = await _reference_root_for_path(
        session,
        lexical_path=destination_lexical,
        resolved_path=resolved_parent / destination.name,
        require_managed_writes=False,
    )
    unavailable_reason = source_reason or destination_reason
    if not unavailable_reason and os.path.lexists(destination):
        unavailable_reason = (
            "The Mylar-recorded destination already exists, so Pullbox left it unchanged."
        )
    if not unavailable_reason and (
        not destination_parent.is_dir() or not os.access(destination_parent, os.W_OK)
    ):
        unavailable_reason = "The Mylar-recorded destination folder is not writable."
    if not unavailable_reason and not os.access(source.parent, os.W_OK):
        unavailable_reason = "The current source folder is not writable."
    return _CleanupContext(
        job,
        imported_file,
        library_file,
        source,
        destination,
        signature,
        unavailable_reason,
    )


async def _load_duplicate_context(
    session: AsyncSession,
    job_id: int,
    file_id: int,
) -> _DuplicateCleanupContext:
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if (
        job.status is not ImportJobStatus.COMPLETED
        or job.source_type is not ImportSourceType.MYLAR3
    ):
        raise ValidationError("Mylar source cleanup is available after a completed Mylar import.")
    imported_file = await session.get(ImportedFile, file_id)
    if imported_file is None or imported_file.import_job_id != job_id:
        raise NotFoundError("ImportedFile", file_id)
    evidence = dict(imported_file.diagnostics or {}).get("mylar3_cross_folder_reconciliation")
    cleaned = dict(imported_file.diagnostics or {}).get("misplaced_source_cleanup")
    if (
        imported_file.status is not ImportedFileStatus.DUPLICATE_FILE
        or not isinstance(evidence, Mapping)
        or evidence.get("method") not in _VERIFIED_CROSS_FOLDER_METHODS
        or evidence.get("role") != "identical_duplicate"
        or isinstance(cleaned, Mapping)
    ):
        raise ValidationError(
            "Only an untouched, hash-confirmed misplaced duplicate can be removed."
        )
    if imported_file.duplicate_of_file_id is None or not imported_file.content_hash:
        raise ValidationError("The duplicate no longer has exact canonical-file evidence.")
    canonical = await session.get(ImportedFile, imported_file.duplicate_of_file_id)
    if (
        canonical is None
        or canonical.import_job_id != job_id
        or canonical.content_hash != imported_file.content_hash
    ):
        raise ValidationError("The duplicate no longer has exact canonical-file evidence.")

    source_lexical = _safe_absolute_path(imported_file.file_path, label="duplicate path")
    canonical_lexical = _safe_absolute_path(canonical.file_path, label="canonical path")
    try:
        if source_lexical.is_symlink() or canonical_lexical.is_symlink():
            raise ValidationError("Symlinked files cannot use duplicate source cleanup.")
        source = source_lexical.resolve(strict=True)
        canonical_source = canonical_lexical.resolve(strict=True)
        signature = build_file_identity_signature(source)
        canonical_signature = build_file_identity_signature(canonical_source)
        validate_file_identity_signature(dict(imported_file.source_signature or {}), signature)
        validate_file_identity_signature(
            dict(canonical.source_signature or {}), canonical_signature
        )
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError(
            "The duplicate or canonical source changed after import. Leave both files unchanged."
        ) from exc
    unavailable_reason = ""
    _root, root_reason = await _reference_root_for_path(
        session,
        lexical_path=source_lexical,
        resolved_path=source,
        require_managed_writes=True,
    )
    unavailable_reason = root_reason
    actual_hash = await asyncio.to_thread(compute_content_hash, str(source))
    canonical_hash = await asyncio.to_thread(compute_content_hash, str(canonical_source))
    if not unavailable_reason and (
        actual_hash is None
        or canonical_hash is None
        or actual_hash != imported_file.content_hash
        or canonical_hash != imported_file.content_hash
    ):
        unavailable_reason = "The two files are no longer byte-identical, so Pullbox left both."
    configured_trash = await load_import_utility_trash_folder(session)
    settings = get_settings()
    trash_dir = resolve_trash_directory(
        configured_trash,
        library_root=settings.library_root,
        data_dir=settings.data_dir,
    )
    if trash_dir is None:
        unavailable_reason = unavailable_reason or (
            "Configure the Trash folder in Media Management before removing duplicates."
        )
    return _DuplicateCleanupContext(
        job,
        imported_file,
        canonical,
        source,
        signature,
        canonical_signature,
        imported_file.content_hash,
        trash_dir,
        unavailable_reason,
    )


async def preview_misplaced_source_cleanup(
    session: AsyncSession,
    job_id: int,
    file_id: int,
    action: MisplacedSourceCleanupAction,
    *,
    actor_id: int,
) -> MisplacedSourceCleanupPreview:
    """Preview one exact cross-folder cleanup without mutating state."""
    if action is MisplacedSourceCleanupAction.RESTORE_RECORDED_PATH:
        context = await _load_restore_context(session, job_id, file_id)
        destination = context.destination
        already_restored = destination is None
        token = None
        if not context.unavailable_reason and not already_restored:
            token = str(
                _serializer().dumps(
                    {
                        "job_id": job_id,
                        "file_id": file_id,
                        "actor_id": actor_id,
                        "action": action.value,
                        "source": str(context.source),
                        "destination": str(destination),
                        "signature": context.signature,
                    }
                )
            )
        return MisplacedSourceCleanupPreview(
            job_id=job_id,
            file_id=file_id,
            action=action,
            file_name=context.imported_file.file_name,
            source_path=str(context.source),
            destination_path=str(destination) if destination is not None else None,
            can_apply=token is not None,
            unavailable_reason=(
                "This file is already at the Mylar-recorded path."
                if already_restored
                else context.unavailable_reason
            ),
            preview_token=token,
        )
    if action is MisplacedSourceCleanupAction.TRASH_IDENTICAL_DUPLICATE:
        duplicate = await _load_duplicate_context(session, job_id, file_id)
        token = None
        if not duplicate.unavailable_reason and duplicate.trash_dir is not None:
            token = str(
                _serializer().dumps(
                    {
                        "job_id": job_id,
                        "file_id": file_id,
                        "actor_id": actor_id,
                        "action": action.value,
                        "source": str(duplicate.source),
                        "trash_dir": str(duplicate.trash_dir),
                        "signature": duplicate.signature,
                        "canonical_signature": duplicate.canonical_signature,
                        "content_hash": duplicate.content_hash,
                    }
                )
            )
        return MisplacedSourceCleanupPreview(
            job_id=job_id,
            file_id=file_id,
            action=action,
            file_name=duplicate.imported_file.file_name,
            source_path=str(duplicate.source),
            destination_path=str(duplicate.trash_dir) if duplicate.trash_dir is not None else None,
            can_apply=token is not None,
            unavailable_reason=duplicate.unavailable_reason,
            preview_token=token,
        )
    raise ValidationError("This misplaced source cleanup action is not supported.")


async def _load_verified_restore_contexts(
    session: AsyncSession,
    job_id: int,
) -> tuple[list[_CleanupContext], int]:
    file_ids = list(
        await session.scalars(
            select(ImportedFile.id)
            .where(
                *_cleanup_scope_filters(
                    job_id,
                    MisplacedSourceCleanupAction.RESTORE_RECORDED_PATH,
                )
            )
            .order_by(ImportedFile.id)
        )
    )
    contexts: list[_CleanupContext] = []
    unavailable_count = 0
    for file_id in file_ids:
        try:
            context = await _load_restore_context(session, job_id, int(file_id))
        except ValidationError:
            unavailable_count += 1
            continue
        if context.unavailable_reason or context.destination is None:
            unavailable_count += 1
            continue
        contexts.append(context)
    return contexts, unavailable_count


def _restore_scope_digest(contexts: list[_CleanupContext]) -> str:
    digest = sha256()
    for context in contexts:
        destination = context.destination
        if destination is None:
            continue
        digest.update(
            (
                f"{context.imported_file.id}\0{context.source}\0{destination}\0"
                f"{sorted(context.signature.items())}\n"
            ).encode()
        )
    return digest.hexdigest()


async def _move_restore_source(context: _CleanupContext) -> Path:
    if context.destination is None:
        raise ValidationError("This file is already at the Mylar-recorded path.")
    await asyncio.to_thread(shutil.move, str(context.source), str(context.destination))
    return context.destination.resolve(strict=True)


async def _update_restore_registration(
    session: AsyncSession,
    context: _CleanupContext,
    final_path: Path,
) -> None:
    final_signature = build_file_identity_signature(final_path)
    stat_result = final_path.stat()
    context.imported_file.file_path = str(final_path)
    context.imported_file.file_name = final_path.name
    context.imported_file.source_signature = final_signature
    context.library_file.file_path = str(final_path)
    context.library_file.file_name = final_path.name
    context.library_file.file_size = stat_result.st_size
    context.library_file.file_modified_at = datetime.fromtimestamp(stat_result.st_mtime, UTC)
    context.library_file.source_signature = final_signature
    diagnostics = dict(context.imported_file.diagnostics or {})
    evidence = dict(diagnostics.get("mylar3_cross_folder_reconciliation") or {})
    evidence.update(
        {
            "restored_at": datetime.now(UTC).isoformat(),
            "restored_path": str(final_path),
        }
    )
    diagnostics["mylar3_cross_folder_reconciliation"] = evidence
    context.imported_file.diagnostics = diagnostics
    registration_action = await _load_registration_action(
        session,
        job_id=context.job.id,
        imported_file_id=context.imported_file.id,
    )
    if registration_action is not None:
        registration_payload = dict(registration_action.payload or {})
        registration_payload.update(
            {
                "destination_path": str(final_path),
                "destination_signature": final_signature,
                "original_source_path": str(final_path),
            }
        )
        registration_action.payload = registration_payload


async def _restore_source_moves(moved: list[tuple[Path, Path]]) -> None:
    for source, destination in reversed(moved):
        if os.path.lexists(destination) and not os.path.lexists(source):
            await asyncio.to_thread(shutil.move, str(destination), str(source))


async def preview_verified_misplaced_source_cleanup(
    session: AsyncSession,
    job_id: int,
    *,
    actor_id: int,
) -> MisplacedSourceCleanupBulkPreview:
    """Preview every currently eligible exact misplaced-file restoration."""
    await _load_completed_mylar_job(session, job_id)
    contexts, unavailable_count = await _load_verified_restore_contexts(session, job_id)
    token = None
    if contexts:
        token = str(
            _serializer().dumps(
                {
                    "job_id": job_id,
                    "actor_id": actor_id,
                    "action": "restore_all_verified",
                    "affected_count": len(contexts),
                    "unavailable_count": unavailable_count,
                    "scope_digest": _restore_scope_digest(contexts),
                }
            )
        )
    return MisplacedSourceCleanupBulkPreview(
        job_id=job_id,
        affected_count=len(contexts),
        unavailable_count=unavailable_count,
        examples=tuple(context.imported_file.file_name for context in contexts[:3]),
        preview_token=token,
    )


async def apply_verified_misplaced_source_cleanup(
    session: AsyncSession,
    job_id: int,
    *,
    actor_id: int,
    preview_token: str,
    actor_username: str | None = None,
    source_ip: str | None = None,
) -> MisplacedSourceCleanupBulkResult:
    """Move every file covered by a signed exact-scope preview."""
    payload = _load_token(preview_token)
    contexts, unavailable_count = await _load_verified_restore_contexts(session, job_id)
    if (
        payload.get("job_id") != job_id
        or payload.get("actor_id") != actor_id
        or payload.get("action") != "restore_all_verified"
        or payload.get("affected_count") != len(contexts)
        or payload.get("unavailable_count") != unavailable_count
        or payload.get("scope_digest") != _restore_scope_digest(contexts)
    ):
        raise ValidationError("The verified-file cleanup scope changed. Preview it again.")
    if not contexts:
        raise ValidationError("No verified misplaced files are currently available to move.")

    moved: list[tuple[Path, Path]] = []
    try:
        for context in contexts:
            destination = context.destination
            if destination is None:
                raise ValidationError("A verified misplaced file is already at its proposed path.")
            final_path = await _move_restore_source(context)
            moved.append((context.source, final_path))
            await _update_restore_registration(session, context, final_path)
        await AuditService.log_event(
            session,
            AuditEventType.IMPORT_MISPLACED_SOURCE_CLEANUP,
            source_ip=source_ip,
            user_id=actor_id,
            username=actor_username,
            detail=f"{len(moved)} verified misplaced Mylar sources were organized.",
            metadata={
                "job_id": job_id,
                "moved_count": len(moved),
                "skipped_count": unavailable_count,
            },
        )
        await session.commit()
    except Exception:
        await session.rollback()
        await _restore_source_moves(moved)
        raise
    return MisplacedSourceCleanupBulkResult(
        moved_count=len(moved),
        skipped_count=unavailable_count,
    )


def _load_token(token: str) -> Mapping[str, object]:
    try:
        payload = _serializer().loads(token, max_age=_TOKEN_MAX_AGE_SECONDS)
    except SignatureExpired as exc:
        raise ValidationError("The Mylar cleanup preview expired. Preview it again.") from exc
    except BadSignature as exc:
        raise ValidationError("The Mylar cleanup preview is invalid. Preview it again.") from exc
    if not isinstance(payload, Mapping):
        raise ValidationError("The Mylar cleanup preview is invalid. Preview it again.")
    return payload


async def _load_registration_action(
    session: AsyncSession,
    *,
    job_id: int,
    imported_file_id: int,
) -> ImportJobAction | None:
    return cast(
        "ImportJobAction | None",
        await session.scalar(
            select(ImportJobAction)
            .where(
                ImportJobAction.import_job_id == job_id,
                ImportJobAction.action_type == "library_file_registered",
                ImportJobAction.payload["imported_file_id"].as_integer() == imported_file_id,
            )
            .order_by(ImportJobAction.id.desc())
            .limit(1)
        ),
    )


async def apply_misplaced_source_cleanup(
    session: AsyncSession,
    job_id: int,
    file_id: int,
    action: MisplacedSourceCleanupAction,
    *,
    actor_id: int,
    preview_token: str,
    actor_username: str | None = None,
    source_ip: str | None = None,
) -> MisplacedSourceCleanupResult:
    """Apply one exact cross-folder cleanup after revalidation."""
    if action is MisplacedSourceCleanupAction.TRASH_IDENTICAL_DUPLICATE:
        duplicate = await _load_duplicate_context(session, job_id, file_id)
        if duplicate.unavailable_reason or duplicate.trash_dir is None:
            raise ValidationError(duplicate.unavailable_reason or "Trash is not configured.")
        payload = _load_token(preview_token)
        expected_signature = payload.get("signature")
        expected_canonical_signature = payload.get("canonical_signature")
        if (
            payload.get("job_id") != job_id
            or payload.get("file_id") != file_id
            or payload.get("actor_id") != actor_id
            or payload.get("action") != action.value
            or payload.get("source") != str(duplicate.source)
            or payload.get("trash_dir") != str(duplicate.trash_dir)
            or payload.get("content_hash") != duplicate.content_hash
            or not isinstance(expected_signature, Mapping)
            or not isinstance(expected_canonical_signature, Mapping)
        ):
            raise ValidationError("The Mylar cleanup preview does not match this file.")
        validate_file_identity_signature(dict(expected_signature), duplicate.signature)
        validate_file_identity_signature(
            dict(expected_canonical_signature), duplicate.canonical_signature
        )
        trash_path = await asyncio.to_thread(
            move_file_to_utility_trash,
            duplicate.source,
            duplicate.trash_dir,
            relative_path=Path("import-results") / str(job_id) / duplicate.source.name,
        )
        try:
            diagnostics = dict(duplicate.imported_file.diagnostics or {})
            diagnostics["misplaced_source_cleanup"] = {
                "action": action.value,
                "completed_at": datetime.now(UTC).isoformat(),
                "trash_path": str(trash_path),
            }
            duplicate.imported_file.diagnostics = diagnostics
            await AuditService.log_event(
                session,
                AuditEventType.IMPORT_MISPLACED_SOURCE_CLEANUP,
                source_ip=source_ip,
                user_id=actor_id,
                username=actor_username,
                detail="One hash-confirmed misplaced Mylar duplicate was moved to Trash.",
                metadata={"job_id": job_id, "file_id": file_id},
            )
            await session.commit()
        except Exception:
            await session.rollback()
            await asyncio.to_thread(
                restore_file_from_utility_trash,
                trash_path,
                duplicate.source,
            )
            raise
        return MisplacedSourceCleanupResult(final_path=trash_path)
    if action is not MisplacedSourceCleanupAction.RESTORE_RECORDED_PATH:
        raise ValidationError("This misplaced source cleanup action is not supported.")
    context = await _load_restore_context(session, job_id, file_id)
    if context.unavailable_reason:
        raise ValidationError(context.unavailable_reason)
    if context.destination is None:
        raise ValidationError("This file is already at the Mylar-recorded path.")
    payload = _load_token(preview_token)
    if (
        payload.get("job_id") != job_id
        or payload.get("file_id") != file_id
        or payload.get("actor_id") != actor_id
        or payload.get("action") != action.value
        or payload.get("source") != str(context.source)
        or payload.get("destination") != str(context.destination)
    ):
        raise ValidationError("The Mylar cleanup preview does not match this file.")
    expected_signature = payload.get("signature")
    if not isinstance(expected_signature, Mapping):
        raise ValidationError("The Mylar cleanup preview is invalid. Preview it again.")
    validate_file_identity_signature(dict(expected_signature), context.signature)

    source = context.source
    moved: list[tuple[Path, Path]] = []
    try:
        final_path = await _move_restore_source(context)
        moved.append((source, final_path))
        await _update_restore_registration(session, context, final_path)
        await AuditService.log_event(
            session,
            AuditEventType.IMPORT_MISPLACED_SOURCE_CLEANUP,
            source_ip=source_ip,
            user_id=actor_id,
            username=actor_username,
            detail="One verified misplaced Mylar source was restored to its recorded path.",
            metadata={"job_id": job_id, "file_id": file_id},
        )
        await session.commit()
    except Exception:
        await session.rollback()
        await _restore_source_moves(moved)
        raise
    return MisplacedSourceCleanupResult(final_path=final_path)
