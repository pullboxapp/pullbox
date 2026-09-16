"""Explicit, resumable initial placements for a newly adopted provider arc.

This is not a second placement executor. It records the exact creation-time
members and delegates each artifact to the existing safe manual sync service.
Nothing runs at startup; adapters invoke it after commit or explicit user retry.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

from sqlalchemy import func, select

from pullbox.database import get_session_factory
from pullbox.models.library import LibraryFile
from pullbox.models.story_arc import (
    IssueStoryArc,
    StoryArc,
    StoryArcLifecycle,
    StoryArcResolutionState,
)
from pullbox.services.story_arc_catalog_types import StoryArcCatalogError
from pullbox.services.story_arc_placement_integration import (
    StoryArcPlacementIntegrationError,
    StoryArcPlacementSyncService,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_KEY = "catalog_initial_placements"
_LOCKS: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
_MAX_ITEMS = 2_000


@dataclass(frozen=True, slots=True)
class StoryArcCatalogPlacementResult:
    total: int
    completed: int
    failed: int
    pending: int


@dataclass(frozen=True, slots=True)
class _Item:
    membership_id: int
    library_file_id: int
    source_hash: str
    state: str = "pending"
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class _Marker:
    expected_revision: int
    policy_hash: str
    items: tuple[_Item, ...]

    @property
    def result(self) -> StoryArcCatalogPlacementResult:
        complete = sum(item.state == "complete" for item in self.items)
        failed = sum(item.state == "failed" for item in self.items)
        return StoryArcCatalogPlacementResult(
            len(self.items), complete, failed, len(self.items) - complete - failed
        )

    def payload(self) -> dict[str, object]:
        result = self.result
        return {
            "schema_version": 1,
            "expected_revision": self.expected_revision,
            "policy_hash": self.policy_hash,
            "items": [asdict(item) for item in self.items],
            "state": "pending" if result.pending else "failed" if result.failed else "complete",
            **asdict(result),
        }


async def initialize_catalog_placements(session: AsyncSession, arc: StoryArc) -> None:
    """Record creation-time available members atomically with the new arc."""
    if arc.policy_snapshot.get("mode") not in {"copy", "hardlink", "symlink"}:
        return
    selected_file = (
        select(func.min(LibraryFile.id))
        .where(LibraryFile.issue_id == IssueStoryArc.issue_id)
        .correlate(IssueStoryArc)
        .scalar_subquery()
    )
    rows = await session.execute(
        select(IssueStoryArc.id, LibraryFile)
        .join(LibraryFile, LibraryFile.id == selected_file)
        .where(
            IssueStoryArc.story_arc_id == arc.id,
            IssueStoryArc.resolution_state == StoryArcResolutionState.RESOLVED,
        )
        .order_by(IssueStoryArc.sequence_number, IssueStoryArc.source_ordinal, IssueStoryArc.id)
        .limit(_MAX_ITEMS + 1)
    )
    items = tuple(_Item(membership_id, file.id, _source_hash(file)) for membership_id, file in rows)
    if len(items) > _MAX_ITEMS:
        raise StoryArcCatalogError("catalog_limit_exceeded", "Too many initial placements")
    _store(arc, _Marker(arc.revision, _hash(arc.policy_snapshot), items))
    await session.flush()


async def run_catalog_initial_placements(
    story_arc_id: int,
    *,
    retry_failed: bool = False,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    batch_size: int = 25,
) -> StoryArcCatalogPlacementResult:
    """Process bounded sequential pages after commit; explicit retry resumes work.

    Current/running work is idempotently resumable after a restart. Failed items
    are retried only when the user explicitly requests it. Per-member failures
    never remove the logical arc or turn canonical acquisition into a failure.
    """
    if isinstance(batch_size, bool) or not 1 <= batch_size <= 100:
        raise ValueError("Initial placement batch size must be between 1 and 100")
    factory = session_factory or get_session_factory()
    lock = _LOCKS.setdefault(story_arc_id, asyncio.Lock())
    async with lock:
        attempted: set[int] = set()
        while True:
            async with factory() as session:
                arc = await _arc(session, story_arc_id)
                marker = _marker(arc)
            pending = [
                item
                for item in marker.items
                if item.membership_id not in attempted
                and (
                    item.state in {"pending", "running"}
                    or (retry_failed and item.state == "failed")
                )
            ][:batch_size]
            if not pending:
                return marker.result
            for item in pending:
                attempted.add(item.membership_id)
                await _run_one(factory, story_arc_id, item.membership_id)
            await asyncio.sleep(0)


async def _run_one(
    factory: async_sessionmaker[AsyncSession], arc_id: int, membership_id: int
) -> None:
    async with factory() as session:
        arc = await _arc(session, arc_id)
        marker = _marker(arc)
        item = next(value for value in marker.items if value.membership_id == membership_id)
        code: str | None = None
        member = await session.get(IssueStoryArc, membership_id)
        file = await session.get(LibraryFile, item.library_file_id)
        selected_file_id = (
            await session.scalar(
                select(func.min(LibraryFile.id)).where(LibraryFile.issue_id == member.issue_id)
            )
            if member is not None
            else None
        )
        if (
            arc.lifecycle is not StoryArcLifecycle.ACTIVE
            or arc.revision != marker.expected_revision
            or _hash(arc.policy_snapshot) != marker.policy_hash
        ):
            code = "initial_placement_review_changed"
        elif (
            member is None
            or member.story_arc_id != arc_id
            or member.resolution_state is not StoryArcResolutionState.RESOLVED
        ):
            code = "initial_placement_member_changed"
        elif (
            file is None
            or selected_file_id != item.library_file_id
            or file.issue_id != member.issue_id
            or _source_hash(file) != item.source_hash
        ):
            code = "initial_placement_source_changed"
        if code is None:
            _store(arc, _replace_item(marker, membership_id, "running", None))
            await session.commit()
            try:
                await StoryArcPlacementSyncService().sync_membership(session, arc_id, membership_id)
            except StoryArcPlacementIntegrationError as exc:
                await session.rollback()
                code = exc.code
            except Exception:
                await session.rollback()
                code = "initial_placement_failed"
        # sync_membership commits its artifact journal and expires ORM objects.
        # Re-read to preserve other diagnostic keys before checkpointing progress.
        session.expire_all()
        arc = await _arc(session, arc_id)
        _store(
            arc, _replace_item(_marker(arc), membership_id, "failed" if code else "complete", code)
        )
        await session.commit()


async def _arc(session: AsyncSession, story_arc_id: int) -> StoryArc:
    arc = await session.get(StoryArc, story_arc_id)
    if arc is None:
        raise StoryArcCatalogError("arc_unavailable", "Story arc is unavailable")
    return arc


def _replace_item(marker: _Marker, membership_id: int, state: str, code: str | None) -> _Marker:
    return replace(
        marker,
        items=tuple(
            replace(item, state=state, error_code=code)
            if item.membership_id == membership_id
            else item
            for item in marker.items
        ),
    )


def _store(arc: StoryArc, marker: _Marker) -> None:
    arc.diagnostics = {**arc.diagnostics, _KEY: marker.payload()}


def _marker(arc: StoryArc) -> _Marker:
    raw = arc.diagnostics.get(_KEY)
    if raw is None:
        return _Marker(arc.revision, _hash(arc.policy_snapshot), ())
    try:
        if not isinstance(raw, dict) or raw["schema_version"] != 1:
            raise ValueError
        revision = raw["expected_revision"]
        policy_hash = raw["policy_hash"]
        values = raw["items"]
        if (
            not _positive_int(revision)
            or not _hash_string(policy_hash)
            or not isinstance(values, list)
            or len(values) > _MAX_ITEMS
        ):
            raise ValueError
        items = []
        for value in values:
            if not isinstance(value, dict):
                raise ValueError
            item = _Item(**value)
            if (
                not _positive_int(item.membership_id)
                or not _positive_int(item.library_file_id)
                or not _hash_string(item.source_hash)
                or item.state not in {"pending", "running", "failed", "complete"}
            ):
                raise ValueError
            if item.error_code is not None and (
                not isinstance(item.error_code, str) or len(item.error_code) > 100
            ):
                raise ValueError
            items.append(item)
        if len({item.membership_id for item in items}) != len(items):
            raise ValueError
        return _Marker(revision, policy_hash, tuple(items))
    except (KeyError, TypeError, ValueError) as exc:
        raise StoryArcCatalogError(
            "initial_placement_state_invalid", "Initial placement state needs review"
        ) from exc


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _hash_string(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _source_hash(file: LibraryFile) -> str:
    return _hash(
        {
            "path": file.file_path,
            "size": file.file_size,
            "modified": file.file_modified_at,
            "hash": file.file_hash,
            "signature": file.source_signature,
        }
    )
