"""Targeted offline recheck of staged import evidence, never source mutations."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import exists, func, or_, select

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.core.file_safety import (
    FileSafetyError,
    get_archive_size_limit_bytes,
    is_dangerous_file_blocking_enabled,
    run_safety_checks,
)
from pullbox.core.filesystem_policy import is_invalid_path_text, resolve_preview_source
from pullbox.core.library_file_ownership import (
    ReferencedFileValidationError,
    build_file_identity_signature,
    validate_file_identity_signature,
)
from pullbox.core.source_metadata import MetadataSignal, SourceMetadata, SourceMetadataExtractor
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportFileHandlingMode,
    ImportJob,
    ImportJobLog,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.library import LibraryRoot
from pullbox.services.import_content_inspection import inspect_import_content
from pullbox.services.import_safety_diagnostics import build_import_safety_diagnostics
from pullbox.services.import_series_match_state import clear_auto_cv_match_fields
from pullbox.services.import_source_metadata import source_metadata_for_import_file

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _preserve_mylar_identity(base: SourceMetadata, fresh: SourceMetadata) -> SourceMetadata:
    updates: dict[str, Any] = {}
    signals = dict(fresh.signals)
    diagnostics = dict(fresh.diagnostics)
    raw_conflicts = diagnostics.get("identity_conflicts")
    conflicts = list(raw_conflicts) if isinstance(raw_conflicts, list) else []
    for field in ("comicvine_series_id", "comicvine_issue_id", "issue_type"):
        value = getattr(base, field)
        if base.signals.get(field) != MetadataSignal.MYLAR3 or value is None:
            continue
        current = getattr(fresh, field)
        if field != "issue_type" and current is not None and current != value:
            conflicts.append({"field": field, "mylar3": value, "source": current})
        updates[field] = value
        signals[field] = MetadataSignal.MYLAR3
    for key in ("mylar3_issue", "mylar3_path_reconciliation", "source_layout"):
        if key in base.diagnostics:
            diagnostics[key] = base.diagnostics[key]
    if conflicts:
        diagnostics["identity_conflicts"] = conflicts
    return fresh.model_copy(update={**updates, "signals": signals, "diagnostics": diagnostics})


def inspect_review_source(
    path: Path,
    base: SourceMetadata,
    signature: dict[str, Any],
    *,
    roots: list[tuple[Path, Path]],
    block_dangerous: bool,
    max_archive_size: int,
    accept_replaced_files: bool,
    sidecars: dict[str, dict[str, Any]],
) -> tuple[SourceMetadata, dict[str, Any], dict[str, int | str]]:
    """Inspect only explicitly permitted paths; no providers or page extraction."""
    current_signature: dict[str, int | str] = {}
    fresh = base
    try:
        if is_invalid_path_text(str(path)) or ".." in path.parts:
            raise ReferencedFileValidationError("source_path_unsafe", "Unsafe source path")
        lexical = path.expanduser().absolute()
        resolved = path.expanduser().resolve(strict=False)
        if not any(
            lexical.is_relative_to(root) and resolved.is_relative_to(real) for root, real in roots
        ):
            raise ReferencedFileValidationError("source_outside_root", "Outside approved root")
        resolve_preview_source(path)
        current_signature = build_file_identity_signature(path)
        if not accept_replaced_files:
            validate_file_identity_signature(signature, current_signature)
        extractor = SourceMetadataExtractor()
        folder = str(path.parent)
        if folder not in sidecars:
            sidecars[folder] = extractor.read_sidecars(path.parent)
        fresh = _preserve_mylar_identity(
            base,
            extractor.from_path(
                path,
                include_archive_comicinfo=False,
                include_archive_entry_issue_hint=False,
                sidecar_data=sidecars[folder],
            ),
        )
        inspection = run_safety_checks(
            path, block_dangerous=block_dangerous, max_archive_size=max_archive_size
        )
        content = inspect_import_content(path, inspection)
        report = next((r for r in inspection.archives if r.archive_path == path), None)
        evidence = (
            None
            if report is None
            else {
                "member_index_scanned": True,
                "comicinfo_entry_count": report.comicinfo_entry_count,
                "comicinfo": asdict(report.comicinfo) if report.comicinfo is not None else None,
                "comicinfo_entry": report.comicinfo_entry,
                "comicinfo_error": report.comicinfo_error,
            }
        )
        fresh = _preserve_mylar_identity(
            base,
            extractor.from_path(
                path,
                sidecar_data=sidecars[folder],
                archive_member_evidence=evidence,
            ),
        )
        # Do not accept evidence from a file replaced during the inspection.
        validate_file_identity_signature(
            dict(current_signature), build_file_identity_signature(path)
        )
        return fresh, content, current_signature
    except (ReferencedFileValidationError, FileSafetyError, OSError, ValueError) as exc:
        code = exc.reason if isinstance(exc, ReferencedFileValidationError) else None
        if isinstance(exc, OSError):
            code = "source_unreadable" if isinstance(exc, PermissionError) else "source_missing"
        reason = exc.reason if isinstance(exc, FileSafetyError) else str(code or "source_missing")
        return (
            fresh,
            {
                "file_safety": build_import_safety_diagnostics(
                    reason, code=code, source="review_recheck"
                )
            },
            {},
        )


def _apply_file(
    file: ImportedFile,
    metadata: SourceMetadata,
    content: dict[str, Any],
    signature: dict[str, int | str],
) -> None:
    diagnostics: dict[str, Any] = {}
    source = {**metadata.diagnostics, **content}
    block = source.pop("file_safety", None)
    diagnostics.update(
        {
            "source_metadata": source,
            "source_issue_type": metadata.issue_type.value,
            "comicvine_series_id": metadata.comicvine_series_id,
            "metadata_signals": {key: value.value for key, value in metadata.signals.items()},
        }
    )
    file.parsed_series = metadata.series_name
    file.parsed_issue_number = metadata.issue_number
    file.parsed_year = metadata.year
    file.comicvine_issue_id = metadata.comicvine_issue_id
    file.has_comicinfo = bool(source.get("has_comicinfo"))
    file.include_in_import = False
    file.matched_issue_id = None
    file.matched_issue_cv_id = None
    file.match_confidence = None
    file.match_method = None
    file.error_message = None
    file.status = ImportedFileStatus.PENDING
    if signature:
        file.source_signature = signature
        file.file_size = int(signature["size"])
    if isinstance(block, dict):
        diagnostics["safety_block"] = block
        file.status = ImportedFileStatus.SAFETY_BLOCKED
        file.error_message = str(block["reason"])
    file.diagnostics = diagnostics


def _apply_completed_file_recheck(
    file: ImportedFile,
    metadata: SourceMetadata,
    content: dict[str, Any],
    signature: dict[str, int | str],
) -> bool:
    """Refresh source evidence without changing the saved import decision."""
    diagnostics = dict(file.diagnostics or {})
    previous_signature = dict(file.source_signature or {})
    source = {**metadata.diagnostics, **content}
    block = source.pop("file_safety", None)
    identity_conflicts = source.get("identity_conflicts")
    if block is None and isinstance(identity_conflicts, list) and identity_conflicts:
        block = build_import_safety_diagnostics(
            "The current source identity conflicts with the file reviewed during import.",
            kind="source_revalidation",
            code="source_identity_changed",
            source="completed_import_recheck",
            overrideable_hint=False,
        )
    checked_at = datetime.now(UTC).isoformat()
    if isinstance(block, dict):
        diagnostics["source_revalidation"] = {
            **block,
            "kind": "source_revalidation",
            "source": "completed_import_recheck",
        }
        diagnostics["source_recheck"] = {
            "checked_at": checked_at,
            "ready_for_retry": False,
        }
        file.error_message = str(block.get("sanitized_reason") or block.get("reason"))
        file.diagnostics = diagnostics
        return False

    diagnostics.pop("source_revalidation", None)
    diagnostics.update(
        {
            "source_metadata": source,
            "source_issue_type": metadata.issue_type.value,
            "comicvine_series_id": metadata.comicvine_series_id,
            "metadata_signals": {key: value.value for key, value in metadata.signals.items()},
            "source_recheck": {
                "checked_at": checked_at,
                "ready_for_retry": True,
            },
        }
    )
    file.parsed_series = metadata.series_name
    file.parsed_issue_number = metadata.issue_number
    file.parsed_year = metadata.year
    file.comicvine_issue_id = metadata.comicvine_issue_id
    file.has_comicinfo = bool(source.get("has_comicinfo"))
    refreshed_signature = dict(signature)
    reference_root_id = previous_signature.get("mylar_reference_root_id")
    if (
        isinstance(reference_root_id, int)
        and not isinstance(reference_root_id, bool)
        and reference_root_id > 0
    ):
        refreshed_signature["mylar_reference_root_id"] = reference_root_id
    file.source_signature = refreshed_signature
    file.file_size = int(signature["size"])
    file.error_message = "Source rechecked and ready to retry."
    file.diagnostics = diagnostics
    return True


async def _retry_source_roots(
    session: AsyncSession,
    job: ImportJob,
) -> list[Path]:
    """Recover only source boundaries already approved by the saved import."""
    candidates: list[Path] = []
    if job.source_type is ImportSourceType.FILESYSTEM:
        candidates.append(Path(job.source_path))
    else:
        candidates.extend(Path(value) for value in dict(job.mylar3_path_map or {}).values())
        signature_rows = await session.scalars(
            select(ImportedFile.source_signature).where(
                ImportedFile.import_job_id == job.id,
                ImportedFile.status == ImportedFileStatus.FAILED,
                ImportedFile.diagnostics["source_revalidation"]["retryable"].as_boolean().is_(True),
            )
        )
        root_ids = {
            root_id
            for signature in signature_rows.all()
            if isinstance(signature, dict)
            and isinstance(
                root_id := signature.get("mylar_reference_root_id"),
                int,
            )
            and not isinstance(root_id, bool)
            and root_id > 0
        }
        if root_ids:
            root_query = select(LibraryRoot).where(
                LibraryRoot.id.in_(root_ids),
                LibraryRoot.enabled.is_(True),
            )
            if job.file_handling_mode is ImportFileHandlingMode.IN_PLACE:
                root_query = root_query.where(LibraryRoot.allow_referenced_registrations.is_(True))
            candidates.extend(Path(root.path) for root in (await session.scalars(root_query)).all())

    roots: list[Path] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        try:
            lexical = candidate.expanduser().absolute()
            resolved = resolve_preview_source(candidate)
        except (OSError, RuntimeError, ValueError):
            continue
        key = (str(lexical), str(resolved))
        if resolved.parent == resolved or key in seen:
            continue
        seen.add(key)
        roots.append(lexical)
    return roots


async def prepare_retryable_failed_sources_for_retry(
    session: AsyncSession,
    job: ImportJob,
) -> dict[str, int]:
    """Revalidate changed failed sources as part of the in-app retry action."""
    retryable_count = int(
        await session.scalar(
            select(func.count(ImportedFile.id)).where(
                ImportedFile.import_job_id == job.id,
                ImportedFile.status == ImportedFileStatus.FAILED,
                ImportedFile.diagnostics["source_revalidation"]["retryable"].as_boolean().is_(True),
            )
        )
        or 0
    )
    if retryable_count == 0:
        return {
            "files_checked": 0,
            "files_prepared": 0,
            "blocked_files": 0,
            "skipped_files": 0,
        }

    roots = await _retry_source_roots(session, job)
    if not roots:
        raise ValidationError(
            "No failed files are safe to retry because their approved source root is unavailable."
        )
    return await prepare_completed_import_file_recheck(
        session,
        job.id,
        source_roots=roots,
        apply=True,
        accept_replaced_files=True,
    )


async def prepare_completed_import_file_recheck(
    session: AsyncSession,
    job_id: int,
    *,
    source_roots: list[Path],
    apply: bool = False,
    series_ids: list[int] | None = None,
    accept_replaced_files: bool = False,
) -> dict[str, int]:
    """Recheck only retryable failed sources from an otherwise completed import."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if job.status != ImportJobStatus.COMPLETED or job.control_request != ImportControlRequest.NONE:
        raise ValidationError("Job must be idle in COMPLETED before a failed-file recheck")
    if not source_roots:
        raise ValidationError("At least one explicit source root is required")
    roots = [(path.expanduser().absolute(), resolve_preview_source(path)) for path in source_roots]
    if any(not real.is_dir() or real.parent == real for _, real in roots):
        raise ValidationError("Source roots must be specific existing directories")

    block_dangerous = await is_dangerous_file_blocking_enabled(session)
    max_archive_size = await get_archive_size_limit_bytes(session)
    report = {
        "files_checked": 0,
        "files_prepared": 0,
        "blocked_files": 0,
        "skipped_files": 0,
    }
    sidecars: dict[str, dict[str, Any]] = {}
    cursor = 0
    while True:
        query = (
            select(ImportedFile, ImportedSeries)
            .join(ImportedSeries, ImportedSeries.id == ImportedFile.import_series_id)
            .where(
                ImportedFile.import_job_id == job_id,
                ImportedFile.status == ImportedFileStatus.FAILED,
                ImportedFile.id > cursor,
                ImportedFile.diagnostics["source_revalidation"]["retryable"].as_boolean().is_(True),
            )
            .order_by(ImportedFile.id)
            .limit(250)
        )
        if series_ids:
            query = query.where(ImportedFile.import_series_id.in_(series_ids))
        rows = list((await session.execute(query)).all())
        if not rows:
            break
        for imported_file, imported_series in rows:
            cursor = int(imported_file.id)
            metadata, content, signature = await asyncio.to_thread(
                inspect_review_source,
                Path(imported_file.file_path),
                source_metadata_for_import_file(imported_series, imported_file),
                dict(imported_file.source_signature or {}),
                roots=roots,
                block_dangerous=block_dangerous,
                max_archive_size=max_archive_size,
                accept_replaced_files=accept_replaced_files,
                sidecars=sidecars,
            )
            report["files_checked"] += 1
            blocked = "file_safety" in content
            report["blocked_files"] += int(blocked)
            report["files_prepared"] += int(not blocked)
            if apply:
                _apply_completed_file_recheck(
                    imported_file,
                    metadata,
                    content,
                    signature,
                )
        if apply:
            await session.flush()

    if apply and report["files_checked"]:
        session.add(
            ImportJobLog(
                import_job_id=job.id,
                level="INFO",
                event="import_failed_sources_rechecked",
                message=(
                    f"Rechecked {report['files_checked']} failed source file(s); "
                    f"{report['files_prepared']} are ready for retry."
                ),
                data={**report, "prepared_at": datetime.now(UTC).isoformat()},
            )
        )
        await session.flush()
    return report


async def prepare_import_recheck(
    session: AsyncSession,
    job_id: int,
    *,
    source_roots: list[Path],
    apply: bool = False,
    series_ids: list[int] | None = None,
    accept_replaced_files: bool = False,
) -> dict[str, int]:
    """Dispatch a source recheck without widening the saved job's safe scope."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if job.status == ImportJobStatus.COMPLETED:
        return await prepare_completed_import_file_recheck(
            session,
            job_id,
            source_roots=source_roots,
            apply=apply,
            series_ids=series_ids,
            accept_replaced_files=accept_replaced_files,
        )
    return await prepare_review_recheck(
        session,
        job_id,
        source_roots=source_roots,
        apply=apply,
        series_ids=series_ids,
        accept_replaced_files=accept_replaced_files,
    )


async def prepare_review_recheck(
    session: AsyncSession,
    job_id: int,
    *,
    source_roots: list[Path],
    apply: bool = False,
    series_ids: list[int] | None = None,
    accept_replaced_files: bool = False,
) -> dict[str, int]:
    """Stage a bounded recheck; caller commits once and restarts the offline app.

    Default scope is automatic identity conflicts. Any existing manual decision
    excludes its entire series. All matching resumes from persisted evidence, not
    a new directory inventory. Dry runs do not dirty the session.
    """
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if job.status != ImportJobStatus.REVIEW or job.control_request != ImportControlRequest.NONE:
        raise ValidationError("Job must be idle in REVIEW before an offline recheck")
    if not source_roots:
        raise ValidationError("At least one explicit source root is required")
    roots = [(path.expanduser().absolute(), resolve_preview_source(path)) for path in source_roots]
    if any(not real.is_dir() or real.parent == real for _, real in roots):
        raise ValidationError("Source roots must be specific existing directories")
    block_dangerous = await is_dangerous_file_blocking_enabled(session)
    max_archive_size = await get_archive_size_limit_bytes(session)
    report = {"files_checked": 0, "blocked_files": 0, "skipped_series": 0, "series_prepared": 0}
    cursor = 0
    while True:
        query = select(ImportedSeries).where(
            ImportedSeries.import_job_id == job_id, ImportedSeries.id > cursor
        )
        query = (
            query.where(ImportedSeries.id.in_(series_ids))
            if series_ids
            else query.where(
                ImportedSeries.status == ImportSeriesStatus.NO_MATCH,
                ImportedSeries.diagnostics["reason"].as_string()
                == "trusted_source_identity_conflict",
            )
        )
        item = await session.scalar(query.order_by(ImportedSeries.id).limit(1))
        if item is None:
            break
        cursor = item.id
        protected = await session.scalar(
            select(
                exists().where(
                    ImportedFile.import_series_id == item.id,
                    or_(
                        ImportedFile.status.not_in(
                            [
                                ImportedFileStatus.NO_MATCH,
                                ImportedFileStatus.PENDING,
                                ImportedFileStatus.SAFETY_BLOCKED,
                            ]
                        ),
                        ImportedFile.match_method.startswith("manual"),
                        ImportedFile.include_in_import.is_(True),
                        ImportedFile.diagnostics["safety_exception"]["allowed_once"]
                        .as_boolean()
                        .is_(True),
                    ),
                )
            )
        )
        if (
            item.user_selected_cv_id is not None
            or item.selected_for_import
            or protected
            or item.status not in {ImportSeriesStatus.NO_MATCH, ImportSeriesStatus.MATCHED}
        ):
            report["skipped_series"] += 1
            continue
        sidecars: dict[str, dict[str, Any]] = {}
        identities: set[tuple[int, MetadataSignal]] = set()
        after = 0
        checked = 0
        while True:
            files = list(
                (
                    await session.scalars(
                        select(ImportedFile)
                        .where(
                            ImportedFile.import_series_id == item.id,
                            ImportedFile.id > after,
                        )
                        .order_by(ImportedFile.id)
                        .limit(250)
                    )
                ).all()
            )
            if not files:
                break
            for file in files:
                metadata, content, signature = await asyncio.to_thread(
                    inspect_review_source,
                    Path(file.file_path),
                    source_metadata_for_import_file(item, file),
                    dict(file.source_signature or {}),
                    roots=roots,
                    block_dangerous=block_dangerous,
                    max_archive_size=max_archive_size,
                    accept_replaced_files=accept_replaced_files,
                    sidecars=sidecars,
                )
                checked += 1
                signal = metadata.signals.get("comicvine_series_id")
                if metadata.comicvine_series_id is not None and signal is not None:
                    identities.add((metadata.comicvine_series_id, signal))
                report["files_checked"] += 1
                report["blocked_files"] += int("file_safety" in content)
                if apply:
                    _apply_file(file, metadata, content, signature)
            after = files[-1].id
            if apply:
                await session.flush()
        if checked:
            report["series_prepared"] += 1
            if apply:
                clear_auto_cv_match_fields(item)
                if len({identity for identity, _signal in identities}) == 1:
                    item.cv_id = next(iter(identities))[0]
                    item.cv_match_method = (
                        "mylar3_cv_id"
                        if any(signal is MetadataSignal.MYLAR3 for _, signal in identities)
                        else "comicinfo_cv_id"
                    )
                item.status = ImportSeriesStatus.PENDING
                item.diagnostics = {
                    "reason": "source_recheck_prepared",
                    "previous_reason": "trusted_source_identity_conflict",
                }
    if apply and report["series_prepared"]:
        job.status = ImportJobStatus.MATCHING
        job.match_completed_at = None
        job.progress_snapshot = {
            **(job.progress_snapshot or {}),
            "review_recheck": report,
            "phase": "matching",
            "progress": 0,
        }
        session.add(
            ImportJobLog(
                import_job_id=job.id,
                level="INFO",
                event="import_review_recheck_prepared",
                message=(
                    "Offline source recheck prepared; local matching resumes on startup "
                    "without a new scan."
                ),
                data={**report, "prepared_at": datetime.now(UTC).isoformat()},
            )
        )
        await session.flush()
    return report
