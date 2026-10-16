"""Resume confirmed files stranded beneath a completed import group."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Select, String, cast, select

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobLog,
    ImportSeriesStatus,
)
from pullbox.services.import_deferred_recovery import refresh_recovered_groups
from pullbox.services.import_story_arc_resolution import refresh_story_arc_entries_for_import_files
from pullbox.services.import_workflow_state import raise_if_job_cancelled

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def pending_file_ids(job_id: int) -> Select[int]:
    """Only already-confirmed targets, never unresolved or unselected siblings."""
    return (
        select(ImportedFile.id)
        .join(ImportedSeries, ImportedSeries.id == ImportedFile.import_series_id)
        .where(
            ImportedFile.import_job_id == job_id,
            ImportedSeries.status == ImportSeriesStatus.IMPORTED,
            ImportedSeries.series_id.is_not(None),
            ImportedSeries.cv_id > 0,
            ImportedFile.status == ImportedFileStatus.CONFIRMED,
            ImportedFile.include_in_import.is_(True),
            ImportedFile.library_file_id.is_(None),
            ImportedFile.error_message.is_(None),
            ImportedFile.conflict_group_id.is_(None),
            ImportedFile.duplicate_group_id.is_(None),
            ImportedFile.duplicate_of_file_id.is_(None),
            ImportedFile.matched_issue_cv_id > 0,
            ImportedFile.diagnostics["target_issue_summary"]["provider_id"].as_string()
            == cast(ImportedFile.matched_issue_cv_id, String),
            ImportedFile.diagnostics["file_safety"]["code"].as_string().is_(None),
            ImportedFile.diagnostics["source_revalidation"]["code"].as_string().is_(None),
        )
    )


async def pending_scope(session: AsyncSession, job_id: int) -> dict[str, str]:
    return {
        str(file_id): updated.isoformat()
        for file_id, updated in await session.execute(
            select(ImportedFile.id, ImportedFile.updated_at)
            .where(ImportedFile.id.in_(pending_file_ids(job_id)))
            .order_by(ImportedFile.id)
        )
    }


async def prepare_pending_files(session: AsyncSession, job: ImportJob) -> None:
    """Checkpoint isolated retry groups together with their selected file rows."""
    snapshot = dict(job.progress_snapshot or {})
    state = dict(snapshot.get("deferred_recovery") or {})
    scope = state.get("pending_files", {})
    ids = list(scope)
    cursor = int(state.get("pending_cursor", 0))
    while cursor < len(ids):
        await raise_if_job_cancelled(session, job.id)
        batch = ids[cursor : cursor + 50]
        rows = (
            await session.execute(
                select(ImportedFile, ImportedSeries)
                .join(ImportedSeries, ImportedSeries.id == ImportedFile.import_series_id)
                .where(
                    ImportedFile.id.in_(pending_file_ids(job.id)),
                    ImportedFile.id.in_([int(value) for value in batch]),
                )
            )
        ).all()
        targets: dict[int, ImportedSeries] = {}
        affected: set[int] = set()
        changed: list[int] = []
        for file, parent in rows:
            if file.updated_at.isoformat() != scope[str(file.id)]:
                continue
            target = targets.get(parent.id)
            if target is None:
                target = ImportedSeries(
                    import_job_id=job.id,
                    raw_series_name=parent.raw_series_name,
                    raw_year=parent.raw_year,
                    cv_id=parent.cv_id,
                    cv_title=parent.cv_title,
                    cv_year=parent.cv_year,
                    cv_match_method="interrupted_recovery",
                    cv_match_score=1.0,
                    series_id=parent.series_id,
                    has_files=True,
                    status=ImportSeriesStatus.CONFIRMED,
                    selected_for_import=True,
                    diagnostics={"kind": "deferred_recovery", "source_preserved": True},
                )
                session.add(target)
                await session.flush()
                targets[parent.id] = target
            affected.update((parent.id, target.id))
            file.import_series_id = target.id
            file.diagnostics = {
                **dict(file.diagnostics or {}),
                "interrupted_recovery": {"source_import_series_id": parent.id},
            }
            changed.append(file.id)
        state["series_ids"] = sorted(
            set(state.get("series_ids", [])) | {target.id for target in targets.values()}
        )
        cursor += len(batch)
        state["pending_cursor"] = cursor
        state["pending_files_prepared"] = state.get("pending_files_prepared", 0) + len(changed)
        await refresh_story_arc_entries_for_import_files(
            session, import_job_id=job.id, import_file_ids=changed
        )
        await refresh_recovered_groups(session, job, affected)
        job.progress_snapshot = {**dict(job.progress_snapshot or {}), "deferred_recovery": state}
        session.add(
            ImportJobLog(
                import_job_id=job.id,
                level="INFO",
                event="import_interrupted_files_prepared",
                message="Resumed selected files from completed import groups.",
                data={"files_prepared": len(changed), "source_preserved": True},
            )
        )
        await session.commit()
