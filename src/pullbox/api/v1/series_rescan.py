"""Start and inspect a saved per-series filesystem reconciliation."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import JSON, Text, cast, func, or_, select, type_coerce, update

from pullbox.api.deps import AuthenticatedUser, DbSession, InteractiveOperatorUser  # noqa: TC001
from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.models.series import Series
from pullbox.utilities.import_guards import ensure_no_active_import_file_mutation
from pullbox.utilities.models import JobState, JobType, UtilityJob, UtilityJobItem
from pullbox.utilities.router import _get_manager, _schedule_dispatch

router = APIRouter(prefix="/series", tags=["series"])
_ACTIVE = [
    JobState.QUEUED,
    JobState.RUNNING,
    JobState.PAUSING,
    JobState.PAUSED,
    JobState.CANCELLING,
]
# These columns store JSON in Text. PostgreSQL needs a JSON cast, while SQLite's
# CAST AS JSON would turn the document into a number rather than preserve it.
_JSON_DOCUMENT = JSON().with_variant(Text(), "sqlite")


def _jobs(series_id: int) -> Any:
    return select(UtilityJob).where(
        UtilityJob.job_type == JobType.SERIES_RESCAN,
        type_coerce(cast(UtilityJob.config, _JSON_DOCUMENT), JSON)["series_id"].as_integer()
        == series_id,
    )


@router.post("/{series_id}/rescan", status_code=202)
async def start_series_rescan(
    series_id: int, session: DbSession, user: InteractiveOperatorUser
) -> dict[str, str]:
    """Queue one read-only scan, reusing an already-active rescan if present."""
    series = await session.get(Series, series_id)
    if series is None:
        raise NotFoundError("Series", series_id)
    if not series.path:
        raise ValidationError(
            "This series has no configured folder. Set its folder before rescanning."
        )
    await ensure_no_active_import_file_mutation(session)
    await session.execute(update(Series).where(Series.id == series_id).values(path=Series.path))
    active = await session.scalar(_jobs(series_id).where(UtilityJob.state.in_(_ACTIVE)).limit(1))
    if active is not None:
        return {"job_id": active.id, "state": active.state}
    manager = _get_manager()
    job = await manager.create_job(
        session,
        JobType.SERIES_RESCAN,
        f"Rescan: {series.title}",
        {"series_id": series_id},
        created_by=user.username,
    )
    await session.commit()
    _schedule_dispatch(manager)
    return {"job_id": job.id, "state": job.state}


@router.get("/{series_id}/rescan")
async def series_rescan_report(
    series_id: int,
    session: DbSession,
    user: AuthenticatedUser,
    job_id: str | None = None,
    page: int = Query(1, ge=1),
) -> dict[str, Any]:
    """Return the latest (or requested) report, with bounded exception pages."""
    query = _jobs(series_id)
    if job_id:
        query = query.where(UtilityJob.id == job_id)
    job = await session.scalar(
        query.order_by(UtilityJob.created_at.desc(), UtilityJob.id.desc()).limit(1)
    )
    if job is None:
        return {"job": None, "items": [], "counts": {}, "page": page, "pages": 0}
    outcome = type_coerce(cast(UtilityJobItem.after_state, _JSON_DOCUMENT), JSON)[
        "outcome"
    ].as_string()
    counts: dict[str, int] = {
        str(label): count
        for label, count in (
            await session.execute(
                select(outcome, func.count())
                .where(
                    UtilityJobItem.job_id == job.id,
                )
                .group_by(outcome)
            )
        ).all()
        if label is not None
    }
    review = or_(outcome == "review", UtilityJobItem.state == "FAILED")
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(UtilityJobItem)
            .where(UtilityJobItem.job_id == job.id, review)
        )
        or 0
    )
    rows = (
        await session.scalars(
            select(UtilityJobItem)
            .where(UtilityJobItem.job_id == job.id, review)
            .order_by(UtilityJobItem.item_index)
            .offset((page - 1) * 25)
            .limit(25)
        )
    ).all()
    if total:
        counts["review"] = total
    return {
        "job": {
            "id": job.id,
            "state": job.state,
            "active": job.state in _ACTIVE,
            "percent": 100
            if job.state == JobState.COMPLETED
            else job.progress_pct
            if job.total_items is not None
            else None,
            "error": job.error_message,
            "total": job.total_items,
        },
        "counts": counts,
        "page": page,
        "pages": (total + 24) // 25,
        "items": [
            {
                "id": row.id,
                "path": row.file_path,
                "reason": row.error_message
                or json.loads(row.after_state or "{}").get("reason", "Review required."),
            }
            for row in rows
        ],
    }
