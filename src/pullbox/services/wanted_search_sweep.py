"""Durable state and fair batching for the global Search Wanted sweep."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from sqlalchemy import and_, case, exists, func, select

from pullbox.models.config import SystemConfig
from pullbox.models.issue import Issue
from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
from pullbox.models.search_log import SearchLog
from pullbox.models.series import Series
from pullbox.services.search_targets import (
    IssueSearchTarget,
    load_wanted_issue_search_targets_by_ids,
    wanted_issue_eligibility_filter,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

WANTED_SEARCH_SWEEP_CONFIG_KEY = "search_wanted_sweep_state"
WANTED_SEARCH_BATCH_DELAY = timedelta(hours=1)
_SWEEP_SCHEMA_VERSION = 1

WantedSearchSweepStatus = Literal["running", "waiting", "completed", "failed"]


@dataclass(frozen=True, slots=True)
class WantedSearchSweepState:
    """Restart-safe snapshot of one complete global wanted-issue sweep."""

    state: WantedSearchSweepStatus
    trigger_type: str
    started_at: datetime
    total_targets: int
    pending_issue_ids: list[int]
    attempted_count: int = 0
    searched_count: int = 0
    skipped_count: int = 0
    sent_count: int = 0
    queued_count: int = 0
    failed_count: int = 0
    batch_number: int = 0
    next_batch_at: datetime | None = None
    completed_at: datetime | None = None
    message: str = "Searching wanted issues"
    schema_version: int = _SWEEP_SCHEMA_VERSION

    @property
    def remaining_count(self) -> int:
        """Return how many snapshotted targets have not been attempted."""
        return len(self.pending_issue_ids)


@dataclass(frozen=True, slots=True)
class WantedSearchBatch:
    """One bounded slice of a durable wanted sweep."""

    issue_ids: list[int]
    targets: list[IssueSearchTarget]
    skipped_issue_ids: list[int]


async def create_wanted_search_sweep(
    session: AsyncSession,
    *,
    trigger_type: str,
    now: datetime | None = None,
) -> WantedSearchSweepState:
    """Snapshot all currently eligible targets in starvation-resistant order."""
    issue_ids = await _load_fair_wanted_issue_ids(session)
    sweep = WantedSearchSweepState(
        state="running",
        trigger_type=trigger_type,
        started_at=now or datetime.now(UTC),
        total_targets=len(issue_ids),
        pending_issue_ids=issue_ids,
    )
    await save_wanted_search_sweep(session, sweep)
    return sweep


async def load_wanted_search_sweep(
    session: AsyncSession,
) -> WantedSearchSweepState | None:
    """Load the current durable sweep, ignoring malformed legacy state."""
    row = await session.get(SystemConfig, WANTED_SEARCH_SWEEP_CONFIG_KEY)
    if row is None:
        return None
    try:
        payload = json.loads(row.value)
        if not isinstance(payload, dict):
            return None
        state = str(payload["state"])
        if state not in {"running", "waiting", "completed", "failed"}:
            return None
        pending = payload.get("pending_issue_ids", [])
        if not isinstance(pending, list):
            return None
        return WantedSearchSweepState(
            state=state,  # type: ignore[arg-type]
            trigger_type=str(payload.get("trigger_type") or "scheduled"),
            started_at=_parse_datetime(payload["started_at"]),
            total_targets=max(0, int(payload.get("total_targets", 0))),
            pending_issue_ids=[int(issue_id) for issue_id in pending],
            attempted_count=max(0, int(payload.get("attempted_count", 0))),
            searched_count=max(0, int(payload.get("searched_count", 0))),
            skipped_count=max(0, int(payload.get("skipped_count", 0))),
            sent_count=max(0, int(payload.get("sent_count", 0))),
            queued_count=max(0, int(payload.get("queued_count", 0))),
            failed_count=max(0, int(payload.get("failed_count", 0))),
            batch_number=max(0, int(payload.get("batch_number", 0))),
            next_batch_at=_parse_optional_datetime(payload.get("next_batch_at")),
            completed_at=_parse_optional_datetime(payload.get("completed_at")),
            message=str(payload.get("message") or "Searching wanted issues"),
            schema_version=int(payload.get("schema_version", _SWEEP_SCHEMA_VERSION)),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


async def save_wanted_search_sweep(
    session: AsyncSession,
    sweep: WantedSearchSweepState,
) -> None:
    """Persist one sweep snapshot in the main database."""
    payload = asdict(sweep)
    for key in ("started_at", "next_batch_at", "completed_at"):
        value = payload[key]
        payload[key] = value.isoformat() if isinstance(value, datetime) else None
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    row = await session.get(SystemConfig, WANTED_SEARCH_SWEEP_CONFIG_KEY)
    if row is None:
        session.add(
            SystemConfig(
                key=WANTED_SEARCH_SWEEP_CONFIG_KEY,
                value=encoded,
                value_type="string",
            )
        )
        return
    row.value = encoded
    row.value_type = "string"


async def load_wanted_search_batch(
    session: AsyncSession,
    sweep: WantedSearchSweepState,
    *,
    limit: int,
) -> WantedSearchBatch:
    """Load the next bounded batch and identify targets no longer eligible."""
    issue_ids = list(sweep.pending_issue_ids[: max(0, limit)])
    targets = await load_wanted_issue_search_targets_by_ids(session, issue_ids)
    eligible_ids = {target.issue_id for target in targets}
    return WantedSearchBatch(
        issue_ids=issue_ids,
        targets=targets,
        skipped_issue_ids=[issue_id for issue_id in issue_ids if issue_id not in eligible_ids],
    )


def mark_wanted_search_batch_running(
    sweep: WantedSearchSweepState,
) -> WantedSearchSweepState:
    """Mark the next durable batch as actively running."""
    return replace(
        sweep,
        state="running",
        next_batch_at=None,
        message="Searching wanted issues",
    )


def pause_wanted_search_sweep(
    sweep: WantedSearchSweepState,
    *,
    now: datetime | None = None,
    message: str = "Paused between batches",
) -> WantedSearchSweepState:
    """Keep the current batch pending and schedule a durable later retry."""
    paused_at = now or datetime.now(UTC)
    return replace(
        sweep,
        state="waiting",
        next_batch_at=paused_at + WANTED_SEARCH_BATCH_DELAY,
        message=message,
    )


def checkpoint_wanted_search_items(
    sweep: WantedSearchSweepState,
    *,
    issue_ids: list[int],
    searched_count: int,
    sent: int,
    queued: int,
    failed: int,
) -> WantedSearchSweepState:
    """Durably consume completed or newly ineligible items within a batch."""
    consumed = set(issue_ids)
    if len(consumed) != len(issue_ids) or not consumed.issubset(sweep.pending_issue_ids):
        raise ValueError("Wanted search checkpoint requires unique pending issue IDs.")
    if searched_count < 0 or searched_count > len(issue_ids):
        raise ValueError("Wanted search checkpoint has an invalid searched count.")
    return replace(
        sweep,
        pending_issue_ids=[
            issue_id for issue_id in sweep.pending_issue_ids if issue_id not in consumed
        ],
        attempted_count=sweep.attempted_count + len(issue_ids),
        searched_count=sweep.searched_count + searched_count,
        skipped_count=sweep.skipped_count + len(issue_ids) - searched_count,
        sent_count=sweep.sent_count + max(0, sent),
        queued_count=sweep.queued_count + max(0, queued),
        failed_count=sweep.failed_count + max(0, failed),
    )


def complete_wanted_search_batch(
    sweep: WantedSearchSweepState,
    *,
    issue_ids: list[int],
    searched_count: int,
    sent: int,
    queued: int,
    failed: int,
    now: datetime | None = None,
) -> WantedSearchSweepState:
    """Advance aggregate counters and either pause or complete the sweep."""
    if issue_ids != sweep.pending_issue_ids[: len(issue_ids)]:
        raise ValueError("Wanted search batch must consume the pending sweep prefix.")
    completed_at = now or datetime.now(UTC)
    pending = list(sweep.pending_issue_ids[len(issue_ids) :])
    is_complete = not pending
    skipped = max(0, len(issue_ids) - searched_count)
    return replace(
        sweep,
        state="completed" if is_complete else "waiting",
        pending_issue_ids=pending,
        attempted_count=sweep.attempted_count + len(issue_ids),
        searched_count=sweep.searched_count + searched_count,
        skipped_count=sweep.skipped_count + skipped,
        sent_count=sweep.sent_count + sent,
        queued_count=sweep.queued_count + queued,
        failed_count=sweep.failed_count + failed,
        batch_number=sweep.batch_number + 1,
        next_batch_at=None if is_complete else completed_at + WANTED_SEARCH_BATCH_DELAY,
        completed_at=completed_at if is_complete else None,
        message="Completed" if is_complete else "Paused between batches",
    )


def wanted_search_sweep_view(sweep: WantedSearchSweepState) -> dict[str, object]:
    """Return a compact JSON-safe progress projection for the Tasks UI."""
    return {
        "state": sweep.state,
        "attempted": sweep.attempted_count,
        "total": sweep.total_targets,
        "remaining": sweep.remaining_count,
        "batch_number": sweep.batch_number,
        "next_batch_at": sweep.next_batch_at.isoformat() if sweep.next_batch_at else None,
        "message": sweep.message,
    }


async def _load_fair_wanted_issue_ids(session: AsyncSession) -> list[int]:
    """Order never-searched targets first, then least-recently searched targets."""
    last_searched_at = (
        select(func.max(SearchLog.created_at))
        .where(SearchLog.issue_id == Issue.id)
        .correlate(Issue)
        .scalar_subquery()
    )
    result = await session.scalars(
        select(Issue.id)
        .join(Series, Series.id == Issue.series_id)
        .where(
            wanted_issue_eligibility_filter(),
            ~exists().where(
                and_(
                    PendingMatch.issue_id == Issue.id,
                    PendingMatch.status == PendingMatchStatus.PENDING,
                )
            ),
        )
        .order_by(
            case((last_searched_at.is_(None), 0), else_=1),
            last_searched_at.asc(),
            Series.sort_title,
            Issue.issue_number,
            Issue.id,
        )
    )
    return [int(issue_id) for issue_id in result.all()]


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Sweep timestamp is missing.")
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _parse_optional_datetime(value: object) -> datetime | None:
    return None if value is None else _parse_datetime(value)
