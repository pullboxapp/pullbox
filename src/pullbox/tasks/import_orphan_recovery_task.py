"""Background runner and live progress state for orphaned-series recovery."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

from pullbox.database import get_session_factory
from pullbox.schemas.import_job import OrphanRecoveryProgressResponse, RecoverOrphanRequest
from pullbox.services.import_job_execution import calculate_import_file_progress_pct

if TYPE_CHECKING:
    from pullbox.models.import_job import ImportedFile

logger = structlog.get_logger(__name__)

_orphan_recovery_states: dict[int, OrphanRecoveryProgressResponse] = {}
_orphan_recovery_start_lock = asyncio.Lock()
_background_tasks: set[asyncio.Task[Any]] = set()


def get_orphan_recovery_progress_state(
    imported_series_id: int,
) -> OrphanRecoveryProgressResponse | None:
    """Return the latest in-memory progress snapshot for one recovery run."""
    return _orphan_recovery_states.get(imported_series_id)


async def start_orphan_recovery_run(
    imported_series_id: int,
    request: RecoverOrphanRequest,
) -> OrphanRecoveryProgressResponse:
    """Start a background orphan-recovery run unless one is already active."""
    async with _orphan_recovery_start_lock:
        existing = _orphan_recovery_states.get(imported_series_id)
        if existing is not None and existing.state == "running":
            return existing

        planned_import_count = sum(
            1 for decision in request.decisions if decision.action == "assign"
        )
        initial = OrphanRecoveryProgressResponse(
            imported_series_id=imported_series_id,
            state="running",
            message=(
                "Preparing recovery import..."
                if planned_import_count > 0
                else "Saving recovery decisions..."
            ),
            total_files=planned_import_count or None,
            skipped_count=sum(1 for decision in request.decisions if decision.action == "skip"),
        )
        _orphan_recovery_states[imported_series_id] = initial

        task = asyncio.create_task(
            _run_orphan_recovery(imported_series_id, request.model_dump(mode="python"))
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return initial


def _set_orphan_recovery_state(
    imported_series_id: int,
    **updates: Any,
) -> OrphanRecoveryProgressResponse:
    current = _orphan_recovery_states.get(
        imported_series_id,
        OrphanRecoveryProgressResponse(imported_series_id=imported_series_id),
    )
    next_state = current.model_copy(update=updates)
    _orphan_recovery_states[imported_series_id] = next_state
    return next_state


async def _queue_orphan_recovery_progress(
    progress: OrphanRecoveryProgressResponse,
) -> None:
    from pullbox.services.operation_progress_dispatch import queue_operation_progress
    from pullbox.services.secondary_operation_progress import (
        build_orphan_recovery_operation_update,
    )

    await queue_operation_progress(build_orphan_recovery_operation_update(progress))


async def _run_orphan_recovery(
    imported_series_id: int,
    request_payload: dict[str, Any],
) -> None:
    from pullbox.composition.services import build_import_service

    session_factory = get_session_factory()
    request = RecoverOrphanRequest.model_validate(request_payload)

    async with session_factory() as session:
        try:
            service = await build_import_service(session)
            job, item = await service._load_orphan_recovery_item(session, imported_series_id)
            progress_settings = {
                "move_to_library": bool(job.move_to_library),
                "convert_to_preferred_format": bool(job.convert_to_preferred_format),
                "update_embedded_comicinfo_from_match": bool(
                    job.update_embedded_comicinfo_from_match
                ),
            }

            async def progress_callback(
                *,
                imp_file: ImportedFile,
                file_index: int,
                total_files: int,
                stage: str,
                current: int,
                total: int,
                unit: str,
                live_only: bool = False,
            ) -> None:
                del live_only
                pct = calculate_import_file_progress_pct(
                    move_to_library=progress_settings["move_to_library"],
                    convert_to_preferred_format=progress_settings["convert_to_preferred_format"],
                    update_embedded_comicinfo_from_match=progress_settings[
                        "update_embedded_comicinfo_from_match"
                    ],
                    imp_file=imp_file,
                    stage=stage,
                    current=current,
                    total=total,
                )
                progress = _set_orphan_recovery_state(
                    imported_series_id,
                    state="running",
                    message=(
                        f"Recovering file {file_index} of {total_files}"
                        if total_files > 0
                        else "Recovering selected files..."
                    ),
                    current_file_name=imp_file.file_name,
                    current_file_stage=stage,
                    current_file_progress_current=current,
                    current_file_progress_total=total,
                    current_file_progress_pct=pct,
                    current_file_progress_unit=unit,
                    file_index=file_index,
                    total_files=total_files,
                )
                await _queue_orphan_recovery_progress(progress)

            preparing = _set_orphan_recovery_state(
                imported_series_id,
                state="running",
                message=f"Preparing recovery for {item.cv_title or item.raw_series_name}...",
            )
            await _queue_orphan_recovery_progress(preparing)
            payload = await service.recover_orphan(
                session,
                imported_series_id,
                request,
                progress_callback=progress_callback,
            )
            await session.commit()
            completed = _set_orphan_recovery_state(
                imported_series_id,
                state="completed",
                message=(
                    "Recovery complete."
                    if payload["status"].value == "imported" or int(payload["files_remaining"]) == 0
                    else "Recovery saved. Some files still need attention."
                ),
                current_file_stage=None,
                current_file_progress_current=None,
                current_file_progress_total=None,
                current_file_progress_pct=None,
                current_file_progress_unit=None,
                result_status=payload["status"],
                imported_count=int(payload["imported_count"]),
                skipped_count=int(payload["skipped_count"]),
                failed_count=int(payload["failed_count"]),
                files_remaining=int(payload["files_remaining"]),
            )
            await _queue_orphan_recovery_progress(completed)
        except Exception as exc:
            logger.exception(
                "orphan_recovery_run_failed",
                imported_series_id=imported_series_id,
            )
            await session.rollback()
            failed = _set_orphan_recovery_state(
                imported_series_id,
                state="failed",
                message="Recovery failed.",
                error_message=str(exc),
                current_file_stage=None,
                current_file_progress_unit=None,
            )
            await _queue_orphan_recovery_progress(failed)
