"""Durable outbox and bounded worker for automatic story-arc synchronization."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import structlog
from sqlalchemy import and_, exists, func, or_, select, update

from pullbox.database import get_session_factory
from pullbox.models.library import LibraryFile
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcLifecycle,
    StoryArcResolutionState,
)
from pullbox.models.story_arc_sync import (
    StoryArcSyncReason,
    StoryArcSyncWork,
    StoryArcSyncWorkState,
)
from pullbox.services.story_arc_placement_integration import (
    STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION,
    StoryArcPlacementIntegrationError,
    StoryArcPlacementSyncService,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.engine import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)

STORY_ARC_SYNC_TASK_ID = "sync_story_arc_placements"
MAX_STORY_ARC_SYNC_BATCH_SIZE = 100
MAX_STORY_ARC_SYNC_ENQUEUE_MEMBERSHIPS = 200
DEFAULT_STORY_ARC_SYNC_BATCH_SIZE = 50
DEFAULT_STORY_ARC_DISCOVERY_LIMIT = 200
_CLAIM_LEASE = timedelta(minutes=15)
_CLAIM_HEARTBEAT_INTERVAL_SECONDS = 60.0
_MAX_ATTEMPTS = 5
_RETRY_DELAYS = (
    timedelta(seconds=30),
    timedelta(minutes=2),
    timedelta(minutes=10),
    timedelta(minutes=30),
    timedelta(hours=1),
)
_POLICY_KEYS = frozenset(
    {
        "schema_version",
        "mode",
        "target_library_root_id",
        "destination_root",
        "folder_template",
        "file_template",
        "symlink_style",
        "synchronize",
    }
)
_RETRYABLE_ERROR_CODES = frozenset(
    {
        "placement_concurrency_conflict",
        "placement_execution_failed",
    }
)


@dataclass(frozen=True, slots=True)
class StoryArcSyncDrainResult:
    """Bounded worker outcome used by task logging and continuation scheduling."""

    discovered: int
    claimed: int
    completed: int
    failed: int
    retrying: int
    cancelled: int
    lost_claims: int
    has_more: bool
    next_retry_at: datetime | None


@dataclass(frozen=True, slots=True)
class _WorkContext:
    work_id: int
    membership_id: int
    story_arc_id: int
    library_file_id: int
    attempt_count: int


def _stable_hash(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _source_signature_hash(library_file: LibraryFile) -> str:
    return _stable_hash(
        {
            "file_path": library_file.file_path,
            "file_size": library_file.file_size,
            "file_modified_at": library_file.file_modified_at,
            "file_hash": library_file.file_hash,
            "source_signature": dict(library_file.source_signature or {}),
        }
    )


def _source_signature_int(library_file: LibraryFile, key: str) -> int | None:
    value = dict(library_file.source_signature or {}).get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _source_signature_path(library_file: LibraryFile) -> str | None:
    value = dict(library_file.source_signature or {}).get("resolved_path")
    return value if isinstance(value, str) else None


def _desired_generation(
    library_file: LibraryFile,
    membership: IssueStoryArc,
    story_arc: StoryArc,
) -> tuple[str, str]:
    source_hash = _source_signature_hash(library_file)
    return (
        _stable_hash(
            {
                "library_file_id": library_file.id,
                "source_signature_hash": source_hash,
                "issue_story_arc_id": membership.id,
                "sequence_number": membership.sequence_number,
                "story_arc_revision": story_arc.revision,
                "policy_schema_version": story_arc.policy_schema_version,
            }
        ),
        source_hash,
    )


def _automatic_sync_enabled(story_arc: StoryArc) -> bool:
    snapshot = dict(story_arc.policy_snapshot or {})
    return bool(
        story_arc.lifecycle is StoryArcLifecycle.ACTIVE
        and story_arc.sync_enabled
        and story_arc.policy_schema_version == STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION
        and set(snapshot) == _POLICY_KEYS
        and snapshot.get("schema_version") == STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION
        and snapshot.get("synchronize") is True
        and snapshot.get("mode") != "logical"
        and isinstance(snapshot.get("destination_root"), str)
        and bool(str(snapshot.get("destination_root")).strip())
        and isinstance(snapshot.get("target_library_root_id"), int)
        and not isinstance(snapshot.get("target_library_root_id"), bool)
    )


async def _eligible_memberships_for_issue(
    session: AsyncSession,
    issue_id: int,
) -> list[tuple[IssueStoryArc, StoryArc]]:
    rows = list(
        (
            await session.execute(
                select(IssueStoryArc, StoryArc)
                .join(StoryArc, IssueStoryArc.story_arc_id == StoryArc.id)
                .where(
                    IssueStoryArc.issue_id == issue_id,
                    IssueStoryArc.sync_eligible.is_(True),
                    IssueStoryArc.resolution_state == StoryArcResolutionState.RESOLVED,
                    StoryArc.lifecycle == StoryArcLifecycle.ACTIVE,
                    StoryArc.sync_enabled.is_(True),
                    StoryArc.policy_schema_version == STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION,
                )
                .order_by(IssueStoryArc.id.asc())
                .limit(MAX_STORY_ARC_SYNC_ENQUEUE_MEMBERSHIPS)
            )
        ).all()
    )
    return [
        (membership, story_arc)
        for membership, story_arc in rows
        if _automatic_sync_enabled(story_arc)
    ]


async def _enqueue_pairs(
    session: AsyncSession,
    library_file: LibraryFile,
    pairs: list[tuple[IssueStoryArc, StoryArc]],
    *,
    reason: StoryArcSyncReason,
) -> int:
    proposals: list[tuple[IssueStoryArc, StoryArc, str, str]] = []
    for membership, story_arc in pairs:
        generation, source_hash = _desired_generation(library_file, membership, story_arc)
        proposals.append((membership, story_arc, generation, source_hash))
    if not proposals:
        return 0

    membership_ids = [membership.id for membership, _arc, _generation, _hash in proposals]
    desired_generations = [generation for _membership, _arc, generation, _hash in proposals]
    existing = set(
        (
            await session.execute(
                select(
                    StoryArcSyncWork.issue_story_arc_id,
                    StoryArcSyncWork.desired_generation,
                )
                .where(
                    StoryArcSyncWork.issue_story_arc_id.in_(membership_ids),
                    StoryArcSyncWork.desired_generation.in_(desired_generations),
                )
                .limit(len(proposals))
            )
        ).all()
    )
    queued = 0
    for membership, story_arc, generation, source_hash in proposals:
        if (membership.id, generation) in existing:
            continue
        session.add(
            StoryArcSyncWork(
                issue_story_arc_id=membership.id,
                library_file_id=library_file.id,
                desired_generation=generation,
                source_signature_hash=source_hash,
                source_file_path=library_file.file_path,
                source_file_size=library_file.file_size,
                source_file_modified_at=library_file.file_modified_at,
                source_file_hash=library_file.file_hash,
                source_signature_schema_version=_source_signature_int(
                    library_file,
                    "schema_version",
                ),
                source_signature_resolved_path=_source_signature_path(library_file),
                source_signature_size=_source_signature_int(library_file, "size"),
                source_signature_mtime_ns=_source_signature_int(library_file, "mtime_ns"),
                source_signature_device=_source_signature_int(library_file, "device"),
                source_signature_inode=_source_signature_int(library_file, "inode"),
                story_arc_revision=story_arc.revision,
                membership_sequence=membership.sequence_number,
                policy_schema_version=story_arc.policy_schema_version
                or STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION,
                reason=reason,
                state=StoryArcSyncWorkState.QUEUED,
            )
        )
        queued += 1
    if queued:
        await session.flush()
    return queued


async def enqueue_story_arc_sync_work(
    session: AsyncSession,
    library_file: LibraryFile,
    *,
    reason: StoryArcSyncReason = StoryArcSyncReason.CANONICAL_REGISTERED,
) -> int:
    """Add DB-only work for every currently eligible arc in the caller's transaction."""
    if library_file.id is None or library_file.issue_id is None:
        return 0
    pairs = await _eligible_memberships_for_issue(session, library_file.issue_id)
    return await _enqueue_pairs(session, library_file, pairs, reason=reason)


async def discover_story_arc_sync_work(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_STORY_ARC_DISCOVERY_LIMIT,
) -> int:
    """Boundedly recover eligible canonical files with no current work generation."""
    if isinstance(limit, bool) or not 1 <= limit <= DEFAULT_STORY_ARC_DISCOVERY_LIMIT:
        raise ValueError(
            f"Story-arc discrepancy limit must be from 1 to {DEFAULT_STORY_ARC_DISCOVERY_LIMIT}"
        )

    current_work = exists().where(
        StoryArcSyncWork.issue_story_arc_id == IssueStoryArc.id,
        StoryArcSyncWork.library_file_id == LibraryFile.id,
        StoryArcSyncWork.story_arc_revision == StoryArc.revision,
        StoryArcSyncWork.membership_sequence == IssueStoryArc.sequence_number,
        StoryArcSyncWork.policy_schema_version == StoryArc.policy_schema_version,
        StoryArcSyncWork.source_file_path == LibraryFile.file_path,
        StoryArcSyncWork.source_file_size == LibraryFile.file_size,
        StoryArcSyncWork.source_file_modified_at == LibraryFile.file_modified_at,
        or_(
            StoryArcSyncWork.source_file_hash == LibraryFile.file_hash,
            and_(
                StoryArcSyncWork.source_file_hash.is_(None),
                LibraryFile.file_hash.is_(None),
            ),
        ),
        or_(
            StoryArcSyncWork.source_signature_schema_version
            == LibraryFile.source_signature["schema_version"].as_integer(),
            and_(
                StoryArcSyncWork.source_signature_schema_version.is_(None),
                LibraryFile.source_signature["schema_version"].as_integer().is_(None),
            ),
        ),
        or_(
            StoryArcSyncWork.source_signature_resolved_path
            == LibraryFile.source_signature["resolved_path"].as_string(),
            and_(
                StoryArcSyncWork.source_signature_resolved_path.is_(None),
                LibraryFile.source_signature["resolved_path"].as_string().is_(None),
            ),
        ),
        or_(
            StoryArcSyncWork.source_signature_size
            == LibraryFile.source_signature["size"].as_integer(),
            and_(
                StoryArcSyncWork.source_signature_size.is_(None),
                LibraryFile.source_signature["size"].as_integer().is_(None),
            ),
        ),
        or_(
            StoryArcSyncWork.source_signature_mtime_ns
            == LibraryFile.source_signature["mtime_ns"].as_integer(),
            and_(
                StoryArcSyncWork.source_signature_mtime_ns.is_(None),
                LibraryFile.source_signature["mtime_ns"].as_integer().is_(None),
            ),
        ),
        or_(
            StoryArcSyncWork.source_signature_device
            == LibraryFile.source_signature["device"].as_integer(),
            and_(
                StoryArcSyncWork.source_signature_device.is_(None),
                LibraryFile.source_signature["device"].as_integer().is_(None),
            ),
        ),
        or_(
            StoryArcSyncWork.source_signature_inode
            == LibraryFile.source_signature["inode"].as_integer(),
            and_(
                StoryArcSyncWork.source_signature_inode.is_(None),
                LibraryFile.source_signature["inode"].as_integer().is_(None),
            ),
        ),
    )
    rows = list(
        (
            await session.execute(
                select(LibraryFile, IssueStoryArc, StoryArc)
                .join(IssueStoryArc, LibraryFile.issue_id == IssueStoryArc.issue_id)
                .join(StoryArc, IssueStoryArc.story_arc_id == StoryArc.id)
                .where(
                    IssueStoryArc.sync_eligible.is_(True),
                    IssueStoryArc.resolution_state == StoryArcResolutionState.RESOLVED,
                    StoryArc.lifecycle == StoryArcLifecycle.ACTIVE,
                    StoryArc.sync_enabled.is_(True),
                    StoryArc.policy_schema_version == STORY_ARC_PLACEMENT_POLICY_SCHEMA_VERSION,
                    ~current_work,
                )
                .order_by(IssueStoryArc.id.asc(), LibraryFile.id.asc())
                .limit(limit)
            )
        ).all()
    )
    queued = 0
    for library_file, membership, story_arc in rows:
        if not _automatic_sync_enabled(story_arc):
            continue
        queued += await _enqueue_pairs(
            session,
            library_file,
            [(membership, story_arc)],
            reason=StoryArcSyncReason.DISCREPANCY_RECOVERY,
        )
    return queued


async def claim_story_arc_sync_work(
    session: AsyncSession,
    work_id: int,
    *,
    now: datetime,
) -> str | None:
    """Atomically lease one ready or stale work row before synchronization I/O."""
    token = secrets.token_urlsafe(24)
    stale_before = now - _CLAIM_LEASE
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(StoryArcSyncWork)
            .where(
                StoryArcSyncWork.id == work_id,
                or_(
                    StoryArcSyncWork.state == StoryArcSyncWorkState.QUEUED,
                    and_(
                        StoryArcSyncWork.state == StoryArcSyncWorkState.RETRY_WAIT,
                        StoryArcSyncWork.next_attempt_at.is_not(None),
                        StoryArcSyncWork.next_attempt_at <= now,
                    ),
                    and_(
                        StoryArcSyncWork.state == StoryArcSyncWorkState.RUNNING,
                        or_(
                            StoryArcSyncWork.claimed_at.is_(None),
                            StoryArcSyncWork.claimed_at <= stale_before,
                        ),
                    ),
                ),
            )
            .values(
                state=StoryArcSyncWorkState.RUNNING,
                attempt_count=StoryArcSyncWork.attempt_count + 1,
                next_attempt_at=None,
                claim_token=token,
                claimed_at=now,
            )
        ),
    )
    await session.commit()
    return token if result.rowcount == 1 else None


async def _refresh_story_arc_sync_claim(
    session: AsyncSession,
    work_id: int,
    claim_token: str,
    *,
    now: datetime,
) -> bool:
    """Refresh one live claim without reviving work owned by another worker."""
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(StoryArcSyncWork)
            .where(
                StoryArcSyncWork.id == work_id,
                StoryArcSyncWork.claim_token == claim_token,
                StoryArcSyncWork.state == StoryArcSyncWorkState.RUNNING,
            )
            .values(claimed_at=now)
        ),
    )
    await session.commit()
    return result.rowcount == 1


async def _maintain_claim_lease(
    session_factory: async_sessionmaker[AsyncSession],
    work_id: int,
    claim_token: str,
    stop_requested: asyncio.Event,
    *,
    interval_seconds: float,
    now_fn: Callable[[], datetime],
) -> None:
    """Heartbeat a live claim while placement I/O runs in a different session."""
    while not stop_requested.is_set():
        try:
            await asyncio.wait_for(stop_requested.wait(), timeout=interval_seconds)
        except TimeoutError:
            try:
                async with session_factory() as heartbeat_session:
                    refreshed = await _refresh_story_arc_sync_claim(
                        heartbeat_session,
                        work_id,
                        claim_token,
                        now=now_fn(),
                    )
            except Exception:
                logger.warning(
                    "story_arc_sync_claim_heartbeat_failed",
                    work_id=work_id,
                    exc_info=True,
                )
                continue
            if not refreshed:
                logger.warning(
                    "story_arc_sync_claim_lost_during_heartbeat",
                    work_id=work_id,
                )
                return


async def _ready_work_ids(
    session: AsyncSession,
    *,
    now: datetime,
    limit: int,
) -> list[int]:
    stale_before = now - _CLAIM_LEASE
    return list(
        (
            await session.scalars(
                select(StoryArcSyncWork.id)
                .where(
                    or_(
                        StoryArcSyncWork.state == StoryArcSyncWorkState.QUEUED,
                        and_(
                            StoryArcSyncWork.state == StoryArcSyncWorkState.RETRY_WAIT,
                            StoryArcSyncWork.next_attempt_at.is_not(None),
                            StoryArcSyncWork.next_attempt_at <= now,
                        ),
                        and_(
                            StoryArcSyncWork.state == StoryArcSyncWorkState.RUNNING,
                            or_(
                                StoryArcSyncWork.claimed_at.is_(None),
                                StoryArcSyncWork.claimed_at <= stale_before,
                            ),
                        ),
                    )
                )
                .order_by(
                    func.coalesce(
                        StoryArcSyncWork.next_attempt_at,
                        StoryArcSyncWork.created_at,
                    ).asc(),
                    StoryArcSyncWork.id.asc(),
                )
                .limit(limit)
            )
        ).all()
    )


async def _load_claimed_context(
    session: AsyncSession,
    work_id: int,
    claim_token: str,
) -> _WorkContext | None:
    row = (
        await session.execute(
            select(StoryArcSyncWork, IssueStoryArc, StoryArc, LibraryFile)
            .join(
                IssueStoryArc,
                StoryArcSyncWork.issue_story_arc_id == IssueStoryArc.id,
            )
            .join(StoryArc, IssueStoryArc.story_arc_id == StoryArc.id)
            .join(LibraryFile, StoryArcSyncWork.library_file_id == LibraryFile.id)
            .where(
                StoryArcSyncWork.id == work_id,
                StoryArcSyncWork.claim_token == claim_token,
                StoryArcSyncWork.state == StoryArcSyncWorkState.RUNNING,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    work, membership, story_arc, library_file = row
    if (
        membership.issue_id is None
        or membership.issue_id != library_file.issue_id
        or not membership.sync_eligible
        or membership.resolution_state is not StoryArcResolutionState.RESOLVED
        or not _automatic_sync_enabled(story_arc)
        or work.desired_generation != _desired_generation(library_file, membership, story_arc)[0]
    ):
        return None
    return _WorkContext(
        work_id=work.id,
        membership_id=membership.id,
        story_arc_id=story_arc.id,
        library_file_id=library_file.id,
        attempt_count=work.attempt_count,
    )


async def _finish_work(
    session: AsyncSession,
    context: _WorkContext,
    claim_token: str,
    *,
    outcome: str,
) -> bool:
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(StoryArcSyncWork)
            .where(
                StoryArcSyncWork.id == context.work_id,
                StoryArcSyncWork.claim_token == claim_token,
                StoryArcSyncWork.state == StoryArcSyncWorkState.RUNNING,
            )
            .values(
                state=StoryArcSyncWorkState.COMPLETED,
                claim_token=None,
                claimed_at=None,
                next_attempt_at=None,
                last_error_code=None,
                last_error_category=None,
                last_error_detail=None,
                last_result={"schema_version": 1, "outcome": outcome},
            )
        ),
    )
    await session.commit()
    return result.rowcount == 1


async def _cancel_work(
    session: AsyncSession,
    work_id: int,
    claim_token: str,
    *,
    code: str,
) -> bool:
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(StoryArcSyncWork)
            .where(
                StoryArcSyncWork.id == work_id,
                StoryArcSyncWork.claim_token == claim_token,
                StoryArcSyncWork.state == StoryArcSyncWorkState.RUNNING,
            )
            .values(
                state=StoryArcSyncWorkState.CANCELLED,
                claim_token=None,
                claimed_at=None,
                next_attempt_at=None,
                last_error_code=code,
                last_error_category="superseded",
                last_error_detail="Automatic story-arc synchronization is no longer eligible.",
            )
        ),
    )
    await session.commit()
    return result.rowcount == 1


def _is_retryable(exc: StoryArcPlacementIntegrationError) -> bool:
    return exc.code in _RETRYABLE_ERROR_CODES or exc.category in {"operation", "cancelled"}


async def _fail_or_retry_work(
    session: AsyncSession,
    context: _WorkContext,
    claim_token: str,
    *,
    now: datetime,
    code: str,
    category: str,
    detail: str,
    retryable: bool,
) -> StoryArcSyncWorkState | None:
    should_retry = retryable and context.attempt_count < _MAX_ATTEMPTS
    state = StoryArcSyncWorkState.RETRY_WAIT if should_retry else StoryArcSyncWorkState.FAILED
    next_attempt_at = (
        now + _RETRY_DELAYS[min(context.attempt_count - 1, len(_RETRY_DELAYS) - 1)]
        if should_retry
        else None
    )
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(StoryArcSyncWork)
            .where(
                StoryArcSyncWork.id == context.work_id,
                StoryArcSyncWork.claim_token == claim_token,
                StoryArcSyncWork.state == StoryArcSyncWorkState.RUNNING,
            )
            .values(
                state=state,
                claim_token=None,
                claimed_at=None,
                next_attempt_at=next_attempt_at,
                last_error_code=code,
                last_error_category=category,
                last_error_detail=detail,
            )
        ),
    )
    await session.commit()
    return state if result.rowcount == 1 else None


async def _next_retry_at(session: AsyncSession) -> datetime | None:
    return await session.scalar(
        select(func.min(StoryArcSyncWork.next_attempt_at)).where(
            StoryArcSyncWork.state == StoryArcSyncWorkState.RETRY_WAIT,
        )
    )


async def _has_ready_work(session: AsyncSession, *, now: datetime) -> bool:
    stale_before = now - _CLAIM_LEASE
    return bool(
        await session.scalar(
            select(StoryArcSyncWork.id)
            .where(
                or_(
                    StoryArcSyncWork.state == StoryArcSyncWorkState.QUEUED,
                    and_(
                        StoryArcSyncWork.state == StoryArcSyncWorkState.RETRY_WAIT,
                        StoryArcSyncWork.next_attempt_at <= now,
                    ),
                    and_(
                        StoryArcSyncWork.state == StoryArcSyncWorkState.RUNNING,
                        or_(
                            StoryArcSyncWork.claimed_at.is_(None),
                            StoryArcSyncWork.claimed_at <= stale_before,
                        ),
                    ),
                )
            )
            .limit(1)
        )
    )


async def process_story_arc_sync_work(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    sync_service: StoryArcPlacementSyncService | Any | None = None,
    batch_size: int = DEFAULT_STORY_ARC_SYNC_BATCH_SIZE,
    discover: bool = True,
    now_fn: Callable[[], datetime] | None = None,
    heartbeat_interval_seconds: float = _CLAIM_HEARTBEAT_INTERVAL_SECONDS,
    heartbeat_now_fn: Callable[[], datetime] | None = None,
) -> StoryArcSyncDrainResult:
    """Discover and process one bounded batch with a fresh session per phase."""
    if isinstance(batch_size, bool) or not 1 <= batch_size <= MAX_STORY_ARC_SYNC_BATCH_SIZE:
        raise ValueError(
            f"Story-arc sync batch size must be from 1 to {MAX_STORY_ARC_SYNC_BATCH_SIZE}"
        )
    if (
        isinstance(heartbeat_interval_seconds, bool)
        or heartbeat_interval_seconds <= 0
        or not math.isfinite(heartbeat_interval_seconds)
    ):
        raise ValueError("Story-arc sync heartbeat interval must be a positive finite number")
    factory = session_factory or get_session_factory()
    service = sync_service or StoryArcPlacementSyncService()
    effective_now_fn = now_fn or (lambda: datetime.now(UTC))
    effective_heartbeat_now_fn = heartbeat_now_fn or effective_now_fn
    discovered = 0
    if discover:
        async with factory() as discovery_session:
            discovered = await discover_story_arc_sync_work(discovery_session)
            await discovery_session.commit()

    async with factory() as list_session:
        work_ids = await _ready_work_ids(
            list_session,
            now=effective_now_fn(),
            limit=batch_size,
        )
        await list_session.rollback()

    claimed = completed = failed = retrying = cancelled = lost_claims = 0
    for work_id in work_ids:
        item_now = effective_now_fn()
        async with factory() as claim_session:
            claim_token = await claim_story_arc_sync_work(
                claim_session,
                work_id,
                now=item_now,
            )
        if claim_token is None:
            continue
        claimed += 1

        async with factory() as context_session:
            context = await _load_claimed_context(context_session, work_id, claim_token)
            await context_session.rollback()
        if context is None:
            async with factory() as result_session:
                cancelled_claim = await _cancel_work(
                    result_session,
                    work_id,
                    claim_token,
                    code="sync_work_superseded",
                )
            if cancelled_claim:
                cancelled += 1
            else:
                lost_claims += 1
            continue

        heartbeat_stop = asyncio.Event()
        heartbeat = asyncio.create_task(
            _maintain_claim_lease(
                factory,
                work_id,
                claim_token,
                heartbeat_stop,
                interval_seconds=heartbeat_interval_seconds,
                now_fn=effective_heartbeat_now_fn,
            )
        )
        try:
            try:
                async with factory() as sync_session:
                    result = await service.sync_membership(
                        sync_session,
                        context.story_arc_id,
                        context.membership_id,
                    )
            finally:
                heartbeat_stop.set()
                await heartbeat
            async with factory() as result_session:
                finished_claim = await _finish_work(
                    result_session,
                    context,
                    claim_token,
                    outcome=result.outcome,
                )
            if finished_claim:
                completed += 1
            else:
                lost_claims += 1
        except asyncio.CancelledError:
            heartbeat_stop.set()
            if not heartbeat.done():
                await heartbeat
            async with factory() as result_session:
                cancelled_state = await _fail_or_retry_work(
                    result_session,
                    context,
                    claim_token,
                    now=effective_now_fn(),
                    code="sync_worker_cancelled",
                    category="cancelled",
                    detail="Automatic story-arc synchronization was interrupted.",
                    retryable=True,
                )
            if cancelled_state is None:
                logger.warning(
                    "story_arc_sync_claim_lost_during_cancellation",
                    work_id=context.work_id,
                    issue_story_arc_id=context.membership_id,
                )
            raise
        except StoryArcPlacementIntegrationError as exc:
            async with factory() as result_session:
                state = await _fail_or_retry_work(
                    result_session,
                    context,
                    claim_token,
                    now=effective_now_fn(),
                    code=exc.code,
                    category=exc.category,
                    detail=str(exc),
                    retryable=_is_retryable(exc),
                )
            if state is None:
                lost_claims += 1
            elif state is StoryArcSyncWorkState.RETRY_WAIT:
                retrying += 1
            else:
                failed += 1
            logger.warning(
                "story_arc_sync_item_failed",
                work_id=context.work_id,
                issue_story_arc_id=context.membership_id,
                error_code=exc.code,
                error_category=exc.category,
                claim_lost=state is None,
                retrying=state is StoryArcSyncWorkState.RETRY_WAIT,
            )
        except Exception:
            async with factory() as result_session:
                state = await _fail_or_retry_work(
                    result_session,
                    context,
                    claim_token,
                    now=effective_now_fn(),
                    code="story_arc_sync_unexpected_failure",
                    category="operation",
                    detail="Automatic story-arc synchronization failed unexpectedly.",
                    retryable=True,
                )
            if state is None:
                lost_claims += 1
            elif state is StoryArcSyncWorkState.RETRY_WAIT:
                retrying += 1
            else:
                failed += 1
            logger.exception(
                "story_arc_sync_item_failed_unexpectedly",
                work_id=context.work_id,
                issue_story_arc_id=context.membership_id,
                claim_lost=state is None,
                retrying=state is StoryArcSyncWorkState.RETRY_WAIT,
            )

    async with factory() as summary_session:
        has_more = await _has_ready_work(summary_session, now=effective_now_fn())
        next_retry_at = await _next_retry_at(summary_session)
        await summary_session.rollback()
    return StoryArcSyncDrainResult(
        discovered=discovered,
        claimed=claimed,
        completed=completed,
        failed=failed,
        retrying=retrying,
        cancelled=cancelled,
        lost_claims=lost_claims,
        has_more=has_more,
        next_retry_at=next_retry_at,
    )


def request_story_arc_sync_now() -> None:
    """Best-effort latency nudge; the durable scheduled sweep remains authoritative."""
    from pullbox.core.scheduler import get_scheduler

    try:
        status = get_scheduler().run_task_now(STORY_ARC_SYNC_TASK_ID)
        if status == "queued":
            logger.debug("story_arc_sync_triggered_after_registration")
    except Exception:
        logger.warning("story_arc_sync_trigger_failed", exc_info=True)
