"""Durable, bounded reinspection and exact stale-reference pairing in review."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from pullbox.core.exceptions import ValidationError
from pullbox.core.file_safety import (
    get_allowed_extensions,
    get_archive_size_limit_bytes,
    is_dangerous_file_blocking_enabled,
)
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.models.library import LibraryFile
from pullbox.models.operation_progress import OperationProgressState, OperationProgressType
from pullbox.services.import_counters import recompute_file_counters, recompute_series_counters
from pullbox.services.import_path_identity import (
    reconciliation_evidence,
    same_trusted_issue,
    unchanged_same_folder_pair,
)
from pullbox.services.import_review_file_assignment import load_review_file
from pullbox.services.import_review_recheck import (
    _apply_file,
    _retry_source_roots,
    inspect_review_source,
)
from pullbox.services.import_review_scope import review_scope
from pullbox.services.import_safety_diagnostics import normalize_import_safety_diagnostics
from pullbox.services.import_source_metadata import source_metadata_for_import_file
from pullbox.services.import_story_arc_resolution import refresh_story_arc_entries_for_import_files
from pullbox.services.operation_progress import (
    OperationProgressMeasure,
    OperationProgressUpdate,
    publish_operation_progress,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

RECHECK_CATEGORIES = frozenset(
    {
        "permission_unreadable",
        "archive_inspection_failed",
        "zero_byte",
        "archive_no_pages",
        "nested_comic_archive",
        "source_changed",
        "source_missing",
        "unsupported_file_type",
        "unknown",
    }
)


async def source_action_progress(
    session: AsyncSession, job_id: int, file_id: int, state: OperationProgressState
) -> None:
    await publish_operation_progress(
        session,
        OperationProgressUpdate(
            operation_type=OperationProgressType.UTILITY,
            operation_key=f"import-review-source:{job_id}:{file_id}",
            group_key=f"import:{job_id}",
            revision=None,
            state=state,
            phase="source_verification",
            title="Import source verification",
            message="Return to review for the result."
            if state.is_terminal
            else "Checking the source and its identity in the background.",
            detail_url=f"/import?resume_job_id={job_id}&resume_step=3",
            overall=OperationProgressMeasure(current=1, total=1, unit="files")
            if state is OperationProgressState.COMPLETED
            else OperationProgressMeasure(),
        ),
    )


MAX_REPLACEMENT_CANDIDATES = 25


async def replacement_candidates(
    session: AsyncSession, file: ImportedFile, parent: ImportedSeries
) -> list[ImportedFile]:
    """Return bounded, exact saved candidates; the worker still verifies the chosen path."""
    safety = normalize_import_safety_diagnostics(
        dict((file.diagnostics or {}).get("safety_block") or {})
    )
    if (
        not file.comicvine_issue_id
        or file.status is not ImportedFileStatus.SAFETY_BLOCKED
        or safety["category"] != "source_missing"
    ):
        return []
    recorded_metadata = source_metadata_for_import_file(parent, file)
    candidate_query = (
        select(ImportedFile)
        .where(
            ImportedFile.import_series_id == parent.id,
            ImportedFile.id != file.id,
            ImportedFile.comicvine_issue_id == file.comicvine_issue_id,
            ImportedFile.file_size > 0,
            ImportedFile.status.in_([ImportedFileStatus.MATCHED, ImportedFileStatus.CONFIRMED]),
        )
        .order_by(ImportedFile.file_name.asc(), ImportedFile.id.asc())
    )
    page_size = MAX_REPLACEMENT_CANDIDATES + 1
    offset = 0
    result: list[ImportedFile] = []
    while len(result) <= MAX_REPLACEMENT_CANDIDATES:
        candidates = list(
            (await session.scalars(candidate_query.offset(offset).limit(page_size))).all()
        )
        if not candidates:
            break
        candidate_paths = [candidate.file_path for candidate in candidates]
        owned_paths = set(
            (
                await session.scalars(
                    select(LibraryFile.file_path).where(LibraryFile.file_path.in_(candidate_paths))
                )
            ).all()
        )
        result.extend(
            candidate
            for candidate in candidates
            if Path(candidate.file_path).parent == Path(file.file_path).parent
            and bool(candidate.source_signature)
            and candidate.file_path not in owned_paths
            and same_trusted_issue(
                recorded_metadata,
                source_metadata_for_import_file(parent, candidate),
            )
        )
        offset += len(candidates)
        if len(candidates) < page_size:
            break
    return result[:MAX_REPLACEMENT_CANDIDATES]


async def replacement_candidate(
    session: AsyncSession, file: ImportedFile, parent: ImportedSeries
) -> ImportedFile | None:
    """Return one candidate only when the saved evidence is unambiguous."""
    candidates = await replacement_candidates(session, file, parent)
    return candidates[0] if len(candidates) == 1 else None


async def queue_source_action(
    session: AsyncSession,
    job_id: int,
    file_id: int,
    *,
    action: str = "recheck",
    actor_id: int = 1,
    candidate_file_id: int | None = None,
) -> None:
    job, parent, file = await load_review_file(session, job_id, file_id)
    category = normalize_import_safety_diagnostics(
        dict((file.diagnostics or {}).get("safety_block") or {})
    )["category"]
    if file.status is not ImportedFileStatus.SAFETY_BLOCKED or category not in RECHECK_CATEGORIES:
        raise ValidationError(
            "This file cannot be rechecked here. Resolve its safety or root setup first."
        )
    if action not in {"recheck", "pair"}:
        raise ValidationError("Unknown source review action.")
    candidates = await replacement_candidates(session, file, parent) if action == "pair" else []
    if action == "pair" and candidate_file_id is None and len(candidates) == 1:
        candidate_file_id = candidates[0].id
    candidate = next((item for item in candidates if item.id == candidate_file_id), None)
    if action == "pair" and candidate is None:
        raise ValidationError("Choose one of the verified replacement files and try again.")
    work = {
        "state": "pending",
        "action": action,
        "actor_id": actor_id,
        "scope": review_scope(job, parent, file),
        "queued_at": datetime.now(UTC).isoformat(),
        "candidate_id": candidate.id if candidate else None,
        "candidate_scope": review_scope(job, parent, candidate) if candidate else None,
    }
    file.diagnostics = {**file.diagnostics, "review_source_action": work}
    parent.diagnostics = {
        **parent.diagnostics,
        "rematch_pending": True,
        "review_source_file_id": file.id,
    }
    await source_action_progress(session, job_id, file_id, OperationProgressState.QUEUED)
    await session.flush()


def _pair_is_unchanged(recorded: Path, actual: Path, signature: dict[str, Any]) -> None:
    if not unchanged_same_folder_pair(recorded, actual, signature):
        raise ValidationError("The recorded path or replacement changed. Recheck the source first.")


def _exact_supported_replacement(source: Path, allowed_extensions: frozenset[str]) -> Path:
    """Find one same-folder, same-stem converted replacement without guessing identity."""
    if source.suffix.lower() in allowed_extensions:
        return source
    matches: list[Path] = []
    try:
        for candidate in source.parent.iterdir():
            if (
                candidate != source
                and candidate.is_file()
                and candidate.stem.casefold() == source.stem.casefold()
                and candidate.suffix.lower() in allowed_extensions
            ):
                matches.append(candidate)
                if len(matches) > 1:
                    return source
    except OSError:
        return source
    return matches[0] if len(matches) == 1 else source


async def process_source_action(
    factory: async_sessionmaker[AsyncSession], job_id: int, file_id: int
) -> int | None:
    """Inspect one archive off-thread with no open database transaction or writer lock."""
    async with factory() as session:
        file = await session.get(ImportedFile, file_id)
        job = await session.get(ImportJob, job_id)
        if file is None or job is None or file.import_job_id != job_id:
            return None
        parent = await session.get(ImportedSeries, file.import_series_id)
        if parent is None:
            return None
        work = dict(file.diagnostics.get("review_source_action") or {})
        if work.get("state") == "matching":
            if (
                job.status is ImportJobStatus.REVIEW
                and job.control_request is ImportControlRequest.NONE
            ):
                return parent.id
            raise ValidationError("This import is no longer accepting review actions.")
        if work.get("state") != "pending":
            return None
        original_scope = review_scope(job, parent, file)
        candidate = (
            await session.get(ImportedFile, work["candidate_id"])
            if work.get("candidate_id")
            else None
        )
        inspected = candidate or file
        base = source_metadata_for_import_file(parent, inspected)
        recorded_base = source_metadata_for_import_file(parent, file)
        source_path = Path(inspected.file_path)
        recorded_path = Path(file.file_path)
        signature = dict(inspected.source_signature)
        roots = await _retry_source_roots(session, job, file_ids=[inspected.id])
        block_dangerous = await is_dangerous_file_blocking_enabled(session)
        limit = await get_archive_size_limit_bytes(session)
        allowed_extensions = frozenset(await get_allowed_extensions(session))
        safety_category = str(
            normalize_import_safety_diagnostics(
                dict((file.diagnostics or {}).get("safety_block") or {})
            )["category"]
        )
        valid = (
            original_scope == work.get("scope")
            and job.status is ImportJobStatus.REVIEW
            and job.control_request is ImportControlRequest.NONE
        )
        if candidate:
            valid = valid and review_scope(job, parent, candidate) == work.get("candidate_scope")
        await source_action_progress(session, job_id, file_id, OperationProgressState.RUNNING)
        await session.commit()
    error = "The review changed while this action was queued. Open it again."
    result = None
    if valid:
        try:
            if candidate:
                await asyncio.to_thread(_pair_is_unchanged, recorded_path, source_path, signature)
            elif safety_category == "unsupported_file_type":
                source_path = await asyncio.to_thread(
                    _exact_supported_replacement, source_path, allowed_extensions
                )
            resolved_roots = await asyncio.to_thread(
                lambda: [(root, root.resolve()) for root in roots]
            )
            result = await asyncio.to_thread(
                inspect_review_source,
                source_path,
                base,
                signature,
                roots=resolved_roots,
                block_dangerous=block_dangerous,
                max_archive_size=limit,
                accept_replaced_files=candidate is None,
                sidecars={},
                allowed_extensions=allowed_extensions,
            )
            if candidate:
                metadata, content, _signature = result
                if "file_safety" in content or not same_trusted_issue(recorded_base, metadata):
                    raise ValidationError(
                        "The replacement did not pass source and exact-identity verification."
                    )
                await asyncio.to_thread(_pair_is_unchanged, recorded_path, source_path, signature)
        except Exception:
            result = None
            error = (
                "Source verification could not complete. "
                "Check access and the replacement, then recheck."
            )
    async with factory() as session:
        file = await session.get(ImportedFile, file_id)
        job = await session.get(ImportJob, job_id)
        if file is None or job is None:
            return None
        parent = await session.get(ImportedSeries, file.import_series_id)
        if parent is None:
            return None
        current_work = file.diagnostics.get("review_source_action") or {}
        if current_work != work:
            return None
        candidate = (
            await session.get(ImportedFile, work["candidate_id"])
            if work.get("candidate_id")
            else None
        )
        if (
            review_scope(job, parent, file) != original_scope
            or job.status is not ImportJobStatus.REVIEW
            or job.control_request is not ImportControlRequest.NONE
        ):
            result = None
        if work.get("candidate_id") and (
            candidate is None or review_scope(job, parent, candidate) != work.get("candidate_scope")
        ):
            result = None
        current_roots = await _retry_source_roots(
            session, job, file_ids=[candidate.id if candidate else file.id]
        )
        if set(current_roots) != set(roots):
            result = None
            error = "The allowed source locations changed. Recheck the import setup first."
        needs_match = False
        if result is not None:
            metadata, content, fresh_signature = result
            if candidate:
                evidence = reconciliation_evidence(
                    file.file_path, candidate.file_path, int(candidate.comicvine_issue_id or 0)
                )
                evidence["recorded_file_id"] = file.id
                file.status = ImportedFileStatus.SKIPPED
                file.include_in_import = False
                file.match_method = "verified_path_reconciliation"
                file.diagnostics = {**file.diagnostics, "mylar3_path_reconciliation": evidence}
                candidate.diagnostics = {
                    **candidate.diagnostics,
                    "mylar3_path_reconciliation": evidence,
                }
            else:
                previous_path = Path(file.file_path)
                previous = dict(file.diagnostics)
                previous_signature = dict(file.source_signature)
                _apply_file(file, metadata, content, fresh_signature)
                if source_path != previous_path:
                    file.file_path = str(source_path)
                    file.file_name = source_path.name
                    file.file_format = source_path.suffix.lower().lstrip(".")
                file.source_signature = {**previous_signature, **file.source_signature}
                file.diagnostics = {
                    **file.diagnostics,
                    "review_source_previous": previous.get("safety_block"),
                    **(
                        {
                            "review_source_replacement": {
                                "previous_file_name": previous_path.name,
                                "replacement_file_name": source_path.name,
                            }
                        }
                        if source_path != previous_path
                        else {}
                    ),
                    **(
                        {"review_selection": previous["review_selection"]}
                        if "review_selection" in previous
                        else {}
                    ),
                }
                if file.status is ImportedFileStatus.PENDING:
                    needs_match = parent.status in {
                        ImportSeriesStatus.MATCHED,
                        ImportSeriesStatus.DUPLICATE,
                    }
                    file.status = (
                        ImportedFileStatus.SAFETY_APPROVED
                        if needs_match
                        else ImportedFileStatus.NO_MATCH
                    )
        file.diagnostics = {
            **file.diagnostics,
            "review_source_action": {
                **work,
                "state": "matching"
                if needs_match
                else "completed"
                if result is not None
                else "failed",
                "error": None if result is not None else error,
                "completed_at": datetime.now(UTC).isoformat(),
            },
        }
        diagnostics = dict(parent.diagnostics)
        if not needs_match and diagnostics.get("review_source_file_id") == file.id:
            diagnostics.pop("rematch_pending", None)
            diagnostics.pop("review_source_file_id", None)
        parent.diagnostics = diagnostics
        await session.flush()
        await recompute_file_counters(session, job, series_ids=[parent.id])
        await recompute_series_counters(session, job)
        await refresh_story_arc_entries_for_import_files(
            session, import_job_id=job_id, import_file_ids=[file.id]
        )
        await source_action_progress(
            session,
            job_id,
            file_id,
            OperationProgressState.RUNNING
            if needs_match
            else OperationProgressState.COMPLETED
            if result is not None
            else OperationProgressState.FAILED,
        )
        await session.commit()
        return parent.id if needs_match else None


async def finish_source_action(
    factory: async_sessionmaker[AsyncSession], job_id: int, file_id: int, *, failed: bool = False
) -> None:
    """Close durable work after matching, including unexpected worker failures."""
    async with factory() as session:
        file = await session.get(ImportedFile, file_id)
        if file is None or file.import_job_id != job_id:
            return
        work = dict(file.diagnostics.get("review_source_action") or {})
        if work.get("state") not in {"pending", "matching"}:
            return
        parent = await session.get(ImportedSeries, file.import_series_id)
        job = await session.get(ImportJob, job_id)
        failed = failed or parent is None or job is None
        if job:
            failed = (
                failed
                or job.status is not ImportJobStatus.REVIEW
                or job.control_request is not ImportControlRequest.NONE
            )
        if parent:
            failed = failed or bool(parent.diagnostics.get("rematch_error"))
            diagnostics = dict(parent.diagnostics)
            if diagnostics.get("review_source_file_id") == file.id:
                diagnostics.pop("rematch_pending", None)
                diagnostics.pop("review_source_file_id", None)
            parent.diagnostics = diagnostics
        file.diagnostics = {
            **file.diagnostics,
            "review_source_action": {
                **work,
                "state": "failed" if failed else "completed",
                "error": "Source matching could not complete. Recheck the source or find its issue."
                if failed
                else None,
                "completed_at": datetime.now(UTC).isoformat(),
            },
        }
        await source_action_progress(
            session,
            job_id,
            file_id,
            OperationProgressState.FAILED if failed else OperationProgressState.COMPLETED,
        )
        await session.commit()
