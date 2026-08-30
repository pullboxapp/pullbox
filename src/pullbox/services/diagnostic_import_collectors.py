"""Bounded aggregate diagnostics for imports and first-class Story Arcs."""

from __future__ import annotations

import enum
import math
import re
import resource
import sys
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobAction,
    ImportJobLog,
    ImportSeriesStatus,
)
from pullbox.models.story_arc import IssueStoryArc, StoryArc, StoryArcPlacement
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.models.story_arc_sync import StoryArcSyncWork
from pullbox.services.import_safety_diagnostics import summarize_import_safety_failures

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

MAX_GROUPS = 32
MAX_RECENT_JOBS = 100
MAX_STEP2_TIMING_ROWS = 100
MAX_CANCEL_EVENT_ROWS = 400
MAX_SAFETY_ROWS = 500
MAX_FAILED_ISSUE_NUMBER_ROWS = 50

_SAFE_CLASS_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,79}$")
_FAILED_RESOLUTION_STATES = ("missing", "ambiguous", "conflict")
_CANCEL_TERMINAL_EVENTS = frozenset({"import_scan_cancelled", "import_cancelled_after_rollback"})
_STEP2_FIELDS = {
    "scan_duration_ms": "scan",
    "analyze_duration_ms": "analyze",
    "series_matching_duration_ms": "series_matching",
    "file_matching_duration_ms": "file_matching",
    "total_duration_ms": "total",
}


def _safe_group_key(value: object) -> str:
    if value is None:
        return "unset"
    if isinstance(value, enum.Enum):
        value = value.value
    if isinstance(value, bool):
        return "true" if value else "false"
    normalized = str(value).strip().lower()
    return normalized if _SAFE_CLASS_RE.fullmatch(normalized) else "other"


async def _group_counts(
    session: AsyncSession,
    model: type[Any],
    column: Any,
    *,
    predicates: Sequence[Any] = (),
    limit: int = MAX_GROUPS,
) -> dict[str, int]:
    """Count values with bounded response cardinality and stable safe labels."""
    count_expression = func.count()
    statement = (
        select(column, count_expression)
        .select_from(model)
        .where(*predicates)
        .group_by(column)
        .order_by(count_expression.desc(), column.asc())
        .limit(limit + 1)
    )
    rows = list((await session.execute(statement)).all())
    counts: dict[str, int] = {}
    for raw_key, raw_count in rows[:limit]:
        key = _safe_group_key(raw_key)
        counts[key] = counts.get(key, 0) + int(raw_count or 0)
    if len(rows) > limit:
        total = int(
            await session.scalar(select(func.count()).select_from(model).where(*predicates)) or 0
        )
        counts["other"] = counts.get("other", 0) + max(total - sum(counts.values()), 0)
    return dict(sorted(counts.items()))


async def _count(
    session: AsyncSession,
    model: type[Any],
    *,
    predicates: Sequence[Any] = (),
) -> int:
    return int(
        await session.scalar(select(func.count()).select_from(model).where(*predicates)) or 0
    )


def _duration_ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None or end < start:
        return None
    return round((end - start).total_seconds() * 1000)


def _percentile(values: Sequence[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(math.ceil(percentile * len(ordered)) - 1, 0)
    return ordered[index]


def _timing_summary(values: Sequence[int]) -> dict[str, int | None]:
    if not values:
        return {
            "sample_count": 0,
            "minimum": None,
            "p50": None,
            "p95": None,
            "average": None,
            "maximum": None,
        }
    return {
        "sample_count": len(values),
        "minimum": min(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "average": round(sum(values) / len(values)),
        "maximum": max(values),
    }


async def _collect_performance(session: AsyncSession) -> dict[str, object]:
    job_rows = list(
        (
            await session.execute(
                select(
                    ImportJob.id,
                    ImportJob.scan_started_at,
                    ImportJob.scan_completed_at,
                    ImportJob.match_started_at,
                    ImportJob.match_completed_at,
                    ImportJob.import_started_at,
                    ImportJob.import_completed_at,
                )
                .order_by(ImportJob.id.desc())
                .limit(MAX_RECENT_JOBS)
            )
        ).all()
    )
    durations: dict[str, list[int]] = defaultdict(list)
    job_ids: list[int] = []
    for job_row in job_rows:
        job_ids.append(int(job_row.id))
        for name, value in (
            ("scan", _duration_ms(job_row.scan_started_at, job_row.scan_completed_at)),
            ("matching", _duration_ms(job_row.match_started_at, job_row.match_completed_at)),
            ("import", _duration_ms(job_row.import_started_at, job_row.import_completed_at)),
            ("total", _duration_ms(job_row.scan_started_at, job_row.import_completed_at)),
        ):
            if value is not None:
                durations[name].append(value)

    step2_values: dict[str, list[int]] = defaultdict(list)
    step2_rows = list(
        (
            await session.scalars(
                select(ImportJobLog.data)
                .where(ImportJobLog.event == "import_step2_timing")
                .order_by(ImportJobLog.id.desc())
                .limit(MAX_STEP2_TIMING_ROWS)
            )
        ).all()
    )
    for data in step2_rows:
        if not isinstance(data, dict):
            continue
        for source_key, output_key in _STEP2_FIELDS.items():
            value = data.get(source_key)
            if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
                step2_values[output_key].append(round(value))

    cancellation_values: list[int] = []
    cancellation_rows_truncated = False
    if job_ids:
        cancel_rows = list(
            (
                await session.execute(
                    select(
                        ImportJobLog.id,
                        ImportJobLog.import_job_id,
                        ImportJobLog.event,
                        ImportJobLog.logged_at,
                    )
                    .where(
                        ImportJobLog.import_job_id.in_(job_ids),
                        ImportJobLog.event.in_(
                            ("import_cancel_requested", *_CANCEL_TERMINAL_EVENTS)
                        ),
                    )
                    .order_by(ImportJobLog.id.desc())
                    .limit(MAX_CANCEL_EVENT_ROWS + 1)
                )
            ).all()
        )
        cancellation_rows_truncated = len(cancel_rows) > MAX_CANCEL_EVENT_ROWS
        events_by_job: dict[int, list[Any]] = defaultdict(list)
        for cancel_row in cancel_rows[:MAX_CANCEL_EVENT_ROWS]:
            events_by_job[int(cancel_row.import_job_id)].append(cancel_row)
        for events in events_by_job.values():
            requested_at: datetime | None = None
            for event in sorted(events, key=lambda item: (item.logged_at, item.id)):
                if event.event == "import_cancel_requested":
                    requested_at = event.logged_at
                elif requested_at is not None and event.event in _CANCEL_TERMINAL_EVENTS:
                    latency = _duration_ms(requested_at, event.logged_at)
                    if latency is not None:
                        cancellation_values.append(latency)
                    requested_at = None

    return {
        "recent_job_window": {
            "limit": MAX_RECENT_JOBS,
            "jobs_sampled": len(job_rows),
        },
        "stage_duration_ms": {
            name: _timing_summary(durations.get(name, []))
            for name in ("scan", "matching", "import", "total")
        },
        "recorded_step2_duration_ms": {
            name: _timing_summary(step2_values.get(name, []))
            for name in ("scan", "analyze", "series_matching", "file_matching", "total")
        },
        "cancellation_latency_ms": {
            **_timing_summary(cancellation_values),
            "event_rows_truncated": cancellation_rows_truncated,
        },
        "resource_snapshot": _resource_snapshot(),
    }


def _resource_snapshot() -> dict[str, int | None]:
    process_rss: int | None = None
    database_size: int | None = None
    wal_size: int | None = None
    try:
        rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        process_rss = rss if sys.platform == "darwin" else rss * 1024
    except Exception:
        pass
    try:
        from pullbox.config import get_settings

        db_url = get_settings().db_url
        if ":///" in db_url:
            db_path = Path(db_url.split(":///", 1)[1])
            database_size = db_path.stat().st_size if db_path.is_file() else None
            wal_path = db_path.with_name(f"{db_path.name}-wal")
            wal_size = wal_path.stat().st_size if wal_path.is_file() else None
    except Exception:
        pass
    return {
        "process_peak_rss_bytes": process_rss,
        "database_file_size_bytes": database_size,
        "sqlite_wal_size_bytes": wal_size,
    }


async def _collect_safety(session: AsyncSession) -> dict[str, object]:
    rows = list(
        (
            await session.scalars(
                select(ImportedFile.diagnostics)
                .where(ImportedFile.status == ImportedFileStatus.SAFETY_BLOCKED)
                .order_by(ImportedFile.id.desc())
                .limit(MAX_SAFETY_ROWS + 1)
            )
        ).all()
    )
    sampled = rows[:MAX_SAFETY_ROWS]
    failures: list[tuple[str, dict[str, object]]] = []
    for diagnostics in sampled:
        if not isinstance(diagnostics, dict):
            continue
        safety_block = diagnostics.get("safety_block")
        if isinstance(safety_block, dict):
            failures.append(("", safety_block))
    return {
        "rows_sampled": len(sampled),
        "sample_limit": MAX_SAFETY_ROWS,
        "sample_truncated": len(rows) > MAX_SAFETY_ROWS,
        "categories": summarize_import_safety_failures(failures, example_limit=0),
    }


def _safe_issue_number(value: object) -> str | None:
    text = str(value).strip()
    if not text or len(text) > 320 or any(ord(char) < 32 for char in text):
        return None
    if text.startswith(("/", "\\", "~")) or "://" in text or ".." in text:
        return None
    return text


async def _collect_failed_issue_numbers(session: AsyncSession) -> dict[str, object]:
    staged = list(
        (
            await session.execute(
                select(
                    ImportedStoryArcEntry.source_issue_number_text,
                    ImportedStoryArcEntry.resolution_state,
                )
                .where(
                    ImportedStoryArcEntry.source_issue_number_text.is_not(None),
                    ImportedStoryArcEntry.resolution_state.in_(_FAILED_RESOLUTION_STATES),
                )
                .order_by(ImportedStoryArcEntry.id.desc())
                .limit(MAX_FAILED_ISSUE_NUMBER_ROWS + 1)
            )
        ).all()
    )
    canonical = list(
        (
            await session.execute(
                select(IssueStoryArc.source_issue_number_text, IssueStoryArc.resolution_state)
                .where(
                    IssueStoryArc.source_issue_number_text.is_not(None),
                    IssueStoryArc.resolution_state.in_(_FAILED_RESOLUTION_STATES),
                )
                .order_by(IssueStoryArc.id.desc())
                .limit(MAX_FAILED_ISSUE_NUMBER_ROWS + 1)
            )
        ).all()
    )
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for raw_number, raw_state in (
        staged[:MAX_FAILED_ISSUE_NUMBER_ROWS] + canonical[:MAX_FAILED_ISSUE_NUMBER_ROWS]
    ):
        number = _safe_issue_number(raw_number)
        if number is not None:
            counts[(number, _safe_group_key(raw_state))] += 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0][0], item[0][1]))
    items = [
        {"issue_number": key[0], "resolution_state": key[1], "count": count}
        for key, count in ordered[:MAX_FAILED_ISSUE_NUMBER_ROWS]
    ]
    return {
        "sample_limit": MAX_FAILED_ISSUE_NUMBER_ROWS,
        "sample_truncated": (
            len(staged) > MAX_FAILED_ISSUE_NUMBER_ROWS
            or len(canonical) > MAX_FAILED_ISSUE_NUMBER_ROWS
            or len(ordered) > MAX_FAILED_ISSUE_NUMBER_ROWS
        ),
        "items": items,
    }


async def collect_import_story_arc_diagnostics(session: AsyncSession) -> dict[str, object]:
    """Return bounded counts and timing evidence without row-level private data."""
    import_total = await _count(session, ImportJob)
    canonical_arc_total = await _count(session, StoryArc)
    staged_arc_total = await _count(session, ImportedStoryArc)
    placement_total = await _count(session, StoryArcPlacement)
    action_total = await _count(session, ImportJobAction)
    sync_total = await _count(session, StoryArcSyncWork)

    failure_event_predicates = (
        or_(
            ImportJobLog.level == "ERROR",
            ImportJobLog.event.like("%failed%"),
            ImportJobLog.event.like("%retry%"),
        ),
    )
    return {
        "schema_version": 1,
        "bounds": {
            "group_limit": MAX_GROUPS,
            "recent_job_limit": MAX_RECENT_JOBS,
            "step2_timing_limit": MAX_STEP2_TIMING_ROWS,
            "cancellation_event_limit": MAX_CANCEL_EVENT_ROWS,
            "safety_row_limit": MAX_SAFETY_ROWS,
            "failed_issue_number_limit": MAX_FAILED_ISSUE_NUMBER_ROWS,
        },
        "imports": {
            "jobs": {
                "total": import_total,
                "by_status": await _group_counts(session, ImportJob, ImportJob.status),
                "by_source_type": await _group_counts(session, ImportJob, ImportJob.source_type),
            },
            "series": {
                "total": await _count(session, ImportedSeries),
                "by_status": await _group_counts(session, ImportedSeries, ImportedSeries.status),
            },
            "files": {
                "total": await _count(session, ImportedFile),
                "by_status": await _group_counts(session, ImportedFile, ImportedFile.status),
            },
            "policy_modes": {
                "file_handling": await _group_counts(
                    session, ImportJob, ImportJob.file_handling_mode
                ),
                "effective_import_strategy": await _group_counts(
                    session, ImportJob, ImportJob.effective_import_strategy
                ),
                "effective_transfer_method": await _group_counts(
                    session, ImportJob, ImportJob.effective_transfer_method
                ),
                "source_preserved": await _group_counts(
                    session, ImportJob, ImportJob.source_preserved
                ),
                "story_arc_import_requested": await _group_counts(
                    session, ImportJob, ImportJob.story_arc_import_requested
                ),
                "story_arc_materialization_requested": await _group_counts(
                    session, ImportJob, ImportJob.story_arc_materialization_requested
                ),
            },
            "safety": await _collect_safety(session),
        },
        "story_arcs": {
            "canonical": {
                "total": canonical_arc_total,
                "by_lifecycle": await _group_counts(session, StoryArc, StoryArc.lifecycle),
                "by_source_kind": await _group_counts(session, StoryArc, StoryArc.source_kind),
                "sync_enabled": await _group_counts(session, StoryArc, StoryArc.sync_enabled),
            },
            "canonical_entries": {
                "total": await _count(session, IssueStoryArc),
                "by_resolution_state": await _group_counts(
                    session, IssueStoryArc, IssueStoryArc.resolution_state
                ),
            },
            "staged": {
                "total": staged_arc_total,
                "by_status": await _group_counts(
                    session, ImportedStoryArc, ImportedStoryArc.status
                ),
                "by_source_kind": await _group_counts(
                    session, ImportedStoryArc, ImportedStoryArc.source_kind
                ),
            },
            "staged_entries": {
                "total": await _count(session, ImportedStoryArcEntry),
                "by_resolution_state": await _group_counts(
                    session,
                    ImportedStoryArcEntry,
                    ImportedStoryArcEntry.resolution_state,
                ),
            },
            "placements": {
                "total": placement_total,
                "by_state": await _group_counts(
                    session, StoryArcPlacement, StoryArcPlacement.state
                ),
                "by_mode": await _group_counts(session, StoryArcPlacement, StoryArcPlacement.mode),
                "by_ownership": await _group_counts(
                    session, StoryArcPlacement, StoryArcPlacement.ownership
                ),
            },
            "failed_issue_numbers": await _collect_failed_issue_numbers(session),
        },
        "performance": await _collect_performance(session),
        "recovery": {
            "control_requests": await _group_counts(session, ImportJob, ImportJob.control_request),
            "jobs_with_story_arc_followup_pending": await _count(
                session,
                ImportJob,
                predicates=(ImportJob.story_arc_placement_followup_pending.is_(True),),
            ),
            "jobs_waiting_for_story_arc_rollback": await _count(
                session,
                ImportJob,
                predicates=(ImportJob.story_arc_rollback_waiting_work_id.is_not(None),),
            ),
            "series_pending_recovery": await _count(
                session,
                ImportedSeries,
                predicates=(ImportedSeries.status == ImportSeriesStatus.RECOVERY_PENDING,),
            ),
            "action_journal": {
                "total": action_total,
                "by_status": await _group_counts(session, ImportJobAction, ImportJobAction.status),
                "by_phase": await _group_counts(session, ImportJobAction, ImportJobAction.phase),
                "by_action_type": await _group_counts(
                    session, ImportJobAction, ImportJobAction.action_type
                ),
            },
            "story_arc_sync_work": {
                "total": sync_total,
                "by_state": await _group_counts(session, StoryArcSyncWork, StoryArcSyncWork.state),
                "by_reason": await _group_counts(
                    session, StoryArcSyncWork, StoryArcSyncWork.reason
                ),
                "claimable": await _group_counts(
                    session, StoryArcSyncWork, StoryArcSyncWork.claimable
                ),
                "cancel_requested": await _count(
                    session,
                    StoryArcSyncWork,
                    predicates=(StoryArcSyncWork.cancel_requested_at.is_not(None),),
                ),
                "attempt_count_total": int(
                    await session.scalar(select(func.sum(StoryArcSyncWork.attempt_count))) or 0
                ),
                "attempt_count_maximum": int(
                    await session.scalar(select(func.max(StoryArcSyncWork.attempt_count))) or 0
                ),
                "failure_categories": await _group_counts(
                    session,
                    StoryArcSyncWork,
                    StoryArcSyncWork.last_error_category,
                    predicates=(StoryArcSyncWork.last_error_category.is_not(None),),
                ),
                "failure_codes": await _group_counts(
                    session,
                    StoryArcSyncWork,
                    StoryArcSyncWork.last_error_code,
                    predicates=(StoryArcSyncWork.last_error_code.is_not(None),),
                ),
            },
            "import_failure_event_classes": await _group_counts(
                session,
                ImportJobLog,
                ImportJobLog.event,
                predicates=failure_event_predicates,
            ),
        },
    }
