"""Explicit private resume and deliberate-completion state transitions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, case, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from pullbox.models.reader import IssueReaderState

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class ReaderStateValidationError(Exception):
    """Raised when a progress write does not match the current content contract."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ReaderStateEventKind(StrEnum):
    """Semantic state changes emitted only after the route commits."""

    COMPLETION_CHANGED = "completion_changed"
    WANT_TO_READ_CHANGED = "want_to_read_changed"


class ReaderCompletionOrigin(StrEnum):
    """How an effective completion transition was initiated."""

    AUTOMATIC = "automatic"
    MANUAL = "manual"
    REREAD = "reread"


@dataclass(frozen=True, slots=True)
class ReaderStateEventDescriptor:
    """Small transport-neutral event returned to the transaction owner."""

    kind: ReaderStateEventKind
    user_id: int
    issue_id: int
    state_version: int
    occurred_at: datetime
    completed: bool | None = None
    want_to_read: bool | None = None
    origin: ReaderCompletionOrigin | None = None


@dataclass(frozen=True, slots=True)
class ReaderStateSnapshot:
    """Detached private reader state safe to use after the DB session closes."""

    user_id: int
    issue_id: int
    last_page_index: int | None
    content_revision: str | None
    page_count: int | None
    progress_updated_at: datetime | None
    last_opened_at: datetime | None
    completed_at: datetime | None
    completion_updated_at: datetime | None
    want_to_read: bool
    want_to_read_updated_at: datetime | None
    state_version: int
    updated_at: datetime

    @property
    def has_progress(self) -> bool:
        return (
            self.last_page_index is not None
            and self.content_revision is not None
            and self.page_count is not None
        )

    @property
    def is_completed(self) -> bool:
        return self.completed_at is not None

    @property
    def is_explicitly_unread(self) -> bool:
        return self.completed_at is None and self.completion_updated_at is not None

    @property
    def position_percent(self) -> int:
        if not self.has_progress or self.last_page_index is None or self.page_count is None:
            return 0
        return max(0, min(100, ((self.last_page_index + 1) * 100) // self.page_count))

    @property
    def is_continue_candidate(self) -> bool:
        return (
            self.has_progress
            and not self.is_completed
            and self.last_page_index is not None
            and self.page_count is not None
            and self.last_page_index < self.page_count - 1
        )


@dataclass(frozen=True, slots=True)
class ReaderStateTransition:
    """Canonical result of one private reader-state command."""

    before: ReaderStateSnapshot | None
    after: ReaderStateSnapshot
    changed: bool
    events: tuple[ReaderStateEventDescriptor, ...]


async def load_reader_state(
    session: AsyncSession,
    *,
    user_id: int,
    issue_id: int,
) -> ReaderStateSnapshot | None:
    """Load one user's state for one issue without mutating it."""
    result = await session.execute(
        select(IssueReaderState).where(
            IssueReaderState.user_id == user_id,
            IssueReaderState.issue_id == issue_id,
        )
    )
    state = result.scalar_one_or_none()
    return snapshot_reader_state(state) if state is not None else None


def _reader_state_insert(session: AsyncSession) -> Any:
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "sqlite":
        return sqlite_insert(IssueReaderState)
    if dialect_name == "postgresql":
        return postgresql_insert(IssueReaderState)
    raise RuntimeError(f"Unsupported reader-state database dialect: {dialect_name}")


async def _execute_state_command(
    session: AsyncSession,
    statement: Any,
    *,
    user_id: int,
    issue_id: int,
) -> tuple[ReaderStateSnapshot, bool]:
    result = await session.execute(
        statement.returning(IssueReaderState).execution_options(populate_existing=True)
    )
    state = result.scalar_one_or_none()
    if state is not None:
        return snapshot_reader_state(state), True
    after = await load_reader_state(
        session,
        user_id=user_id,
        issue_id=issue_id,
    )
    if after is None:  # pragma: no cover - no-op conflicts require an existing row
        raise RuntimeError("Reader state command returned no canonical state.")
    return after, False


async def set_reader_completion(
    session: AsyncSession,
    *,
    user_id: int,
    issue_id: int,
    completed: bool,
) -> ReaderStateTransition:
    """Set explicit completion intent without committing the transaction."""
    before = await load_reader_state(session, user_id=user_id, issue_id=issue_id)
    now = datetime.now(UTC)
    statement = _reader_state_insert(session).values(
        user_id=user_id,
        issue_id=issue_id,
        completed_at=now if completed else None,
        completion_updated_at=now,
        updated_at=now,
    )
    excluded = statement.excluded
    if completed:
        statement = statement.on_conflict_do_update(
            index_elements=[IssueReaderState.user_id, IssueReaderState.issue_id],
            set_={
                "completed_at": case(
                    (IssueReaderState.completed_at.is_(None), excluded.completed_at),
                    else_=IssueReaderState.completed_at,
                ),
                "completion_updated_at": case(
                    (
                        IssueReaderState.completed_at.is_(None),
                        excluded.completion_updated_at,
                    ),
                    else_=IssueReaderState.completion_updated_at,
                ),
                "want_to_read": False,
                "want_to_read_updated_at": case(
                    (
                        IssueReaderState.want_to_read.is_(True),
                        excluded.updated_at,
                    ),
                    else_=IssueReaderState.want_to_read_updated_at,
                ),
                "state_version": IssueReaderState.state_version + 1,
                "updated_at": excluded.updated_at,
            },
            where=or_(
                IssueReaderState.completed_at.is_(None),
                IssueReaderState.want_to_read.is_(True),
            ),
        )
    else:
        statement = statement.on_conflict_do_update(
            index_elements=[IssueReaderState.user_id, IssueReaderState.issue_id],
            set_={
                "completed_at": None,
                "completion_updated_at": excluded.completion_updated_at,
                "state_version": IssueReaderState.state_version + 1,
                "updated_at": excluded.updated_at,
            },
            where=or_(
                IssueReaderState.completed_at.is_not(None),
                IssueReaderState.completion_updated_at.is_(None),
            ),
        )
    after, changed = await _execute_state_command(
        session,
        statement,
        user_id=user_id,
        issue_id=issue_id,
    )
    events: list[ReaderStateEventDescriptor] = []
    if changed and after.completion_updated_at == now:
        events.append(
            ReaderStateEventDescriptor(
                kind=ReaderStateEventKind.COMPLETION_CHANGED,
                user_id=user_id,
                issue_id=issue_id,
                state_version=after.state_version,
                occurred_at=now,
                completed=completed,
                origin=ReaderCompletionOrigin.MANUAL,
            )
        )
    if changed and after.want_to_read_updated_at == now:
        events.append(
            ReaderStateEventDescriptor(
                kind=ReaderStateEventKind.WANT_TO_READ_CHANGED,
                user_id=user_id,
                issue_id=issue_id,
                state_version=after.state_version,
                occurred_at=now,
                want_to_read=False,
            )
        )
    return ReaderStateTransition(
        before=before,
        after=after,
        changed=changed,
        events=tuple(events),
    )


async def set_want_to_read(
    session: AsyncSession,
    *,
    user_id: int,
    issue_id: int,
    enabled: bool,
) -> ReaderStateTransition:
    """Set private queue intent without committing the transaction."""
    before = await load_reader_state(session, user_id=user_id, issue_id=issue_id)
    now = datetime.now(UTC)
    statement = _reader_state_insert(session).values(
        user_id=user_id,
        issue_id=issue_id,
        want_to_read=enabled,
        want_to_read_updated_at=now,
        updated_at=now,
    )
    excluded = statement.excluded
    statement = statement.on_conflict_do_update(
        index_elements=[IssueReaderState.user_id, IssueReaderState.issue_id],
        set_={
            "want_to_read": excluded.want_to_read,
            "want_to_read_updated_at": excluded.want_to_read_updated_at,
            "state_version": IssueReaderState.state_version + 1,
            "updated_at": excluded.updated_at,
        },
        where=or_(
            IssueReaderState.want_to_read != enabled,
            IssueReaderState.want_to_read_updated_at.is_(None),
        ),
    )
    after, changed = await _execute_state_command(
        session,
        statement,
        user_id=user_id,
        issue_id=issue_id,
    )
    events = (
        (
            ReaderStateEventDescriptor(
                kind=ReaderStateEventKind.WANT_TO_READ_CHANGED,
                user_id=user_id,
                issue_id=issue_id,
                state_version=after.state_version,
                occurred_at=now,
                want_to_read=enabled,
            ),
        )
        if changed
        else ()
    )
    return ReaderStateTransition(before=before, after=after, changed=changed, events=events)


async def update_reader_progress(
    session: AsyncSession,
    *,
    user_id: int,
    issue_id: int,
    revision: str,
    page_index: int,
    page_count: int,
    completion_candidate: bool,
    expected_revision: str,
    expected_page_count: int,
    reread_started: bool = False,
) -> ReaderStateTransition:
    """Validate and persist one explicit settled-page update."""
    _validate_update(
        revision=revision,
        page_index=page_index,
        page_count=page_count,
        completion_candidate=completion_candidate,
        reread_started=reread_started,
        expected_revision=expected_revision,
        expected_page_count=expected_page_count,
    )
    before = await load_reader_state(session, user_id=user_id, issue_id=issue_id)
    now = datetime.now(UTC)
    clear_completion = reread_started and before is not None and before.completed_at is not None
    completed_at = now if completion_candidate else None
    completion_updated_at = now if completion_candidate or clear_completion else None
    statement = _reader_state_insert(session).values(
        user_id=user_id,
        issue_id=issue_id,
        last_page_index=page_index,
        content_revision=revision,
        page_count=page_count,
        progress_updated_at=now,
        last_opened_at=now,
        completed_at=completed_at,
        completion_updated_at=completion_updated_at,
        updated_at=now,
    )
    excluded = statement.excluded
    completed_at_update: Any
    completion_updated_at_update: Any
    if clear_completion:
        completed_at_update = None
        completion_updated_at_update = excluded.completion_updated_at
    else:
        completed_at_update = case(
            (
                and_(
                    excluded.completed_at.is_not(None),
                    IssueReaderState.completed_at.is_(None),
                ),
                excluded.completed_at,
            ),
            else_=IssueReaderState.completed_at,
        )
        completion_updated_at_update = case(
            (
                and_(
                    excluded.completed_at.is_not(None),
                    IssueReaderState.completed_at.is_(None),
                ),
                excluded.completion_updated_at,
            ),
            else_=IssueReaderState.completion_updated_at,
        )
    statement = statement.on_conflict_do_update(
        index_elements=[IssueReaderState.user_id, IssueReaderState.issue_id],
        set_={
            "last_page_index": excluded.last_page_index,
            "content_revision": excluded.content_revision,
            "page_count": excluded.page_count,
            "progress_updated_at": excluded.progress_updated_at,
            "last_opened_at": excluded.last_opened_at,
            "completed_at": completed_at_update,
            "completion_updated_at": completion_updated_at_update,
            "want_to_read": case(
                (excluded.completed_at.is_not(None), False),
                else_=IssueReaderState.want_to_read,
            ),
            "want_to_read_updated_at": case(
                (
                    and_(
                        excluded.completed_at.is_not(None),
                        IssueReaderState.want_to_read.is_(True),
                    ),
                    excluded.updated_at,
                ),
                else_=IssueReaderState.want_to_read_updated_at,
            ),
            "state_version": IssueReaderState.state_version + 1,
            "updated_at": excluded.updated_at,
        },
    )
    after, changed = await _execute_state_command(
        session,
        statement,
        user_id=user_id,
        issue_id=issue_id,
    )
    events: list[ReaderStateEventDescriptor] = []
    if clear_completion:
        events.append(
            ReaderStateEventDescriptor(
                kind=ReaderStateEventKind.COMPLETION_CHANGED,
                user_id=user_id,
                issue_id=issue_id,
                state_version=after.state_version,
                occurred_at=now,
                completed=False,
                origin=ReaderCompletionOrigin.REREAD,
            )
        )
    elif not reread_started and after.completion_updated_at == now:
        events.append(
            ReaderStateEventDescriptor(
                kind=ReaderStateEventKind.COMPLETION_CHANGED,
                user_id=user_id,
                issue_id=issue_id,
                state_version=after.state_version,
                occurred_at=now,
                completed=True,
                origin=ReaderCompletionOrigin.AUTOMATIC,
            )
        )
    if after.want_to_read_updated_at == now:
        events.append(
            ReaderStateEventDescriptor(
                kind=ReaderStateEventKind.WANT_TO_READ_CHANGED,
                user_id=user_id,
                issue_id=issue_id,
                state_version=after.state_version,
                occurred_at=now,
                want_to_read=False,
            )
        )
    return ReaderStateTransition(
        before=before,
        after=after,
        changed=changed,
        events=tuple(events),
    )


def _validate_update(
    *,
    revision: str,
    page_index: int,
    page_count: int,
    completion_candidate: bool,
    reread_started: bool,
    expected_revision: str,
    expected_page_count: int,
) -> None:
    if revision != expected_revision:
        raise ReaderStateValidationError("stale_revision", "The comic file has changed.")
    if page_count != expected_page_count or page_count <= 0:
        raise ReaderStateValidationError("page_count_mismatch", "The comic page count changed.")
    if page_index < 0 or page_index >= page_count:
        raise ReaderStateValidationError("page_out_of_range", "The settled page is invalid.")
    if completion_candidate and page_index != page_count - 1:
        raise ReaderStateValidationError(
            "completion_not_final",
            "Completion can only be recorded on the final page.",
        )
    if reread_started and (page_index != 0 or completion_candidate):
        raise ReaderStateValidationError(
            "reread_not_first_page",
            "Rereading must start on page one without completing the issue.",
        )


def snapshot_reader_state(state: IssueReaderState) -> ReaderStateSnapshot:
    """Detach an ORM reader-state row for domain and projection services."""
    return ReaderStateSnapshot(
        user_id=state.user_id,
        issue_id=state.issue_id,
        last_page_index=state.last_page_index,
        content_revision=state.content_revision,
        page_count=state.page_count,
        progress_updated_at=state.progress_updated_at,
        last_opened_at=state.last_opened_at,
        completed_at=state.completed_at,
        completion_updated_at=state.completion_updated_at,
        want_to_read=state.want_to_read,
        want_to_read_updated_at=state.want_to_read_updated_at,
        state_version=state.state_version,
        updated_at=state.updated_at,
    )
