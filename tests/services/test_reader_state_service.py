"""Private reader resume and deliberate-completion service tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.reader import IssueReaderState
from pullbox.services.reader_state_service import (
    ReaderCompletionOrigin,
    ReaderStateEventKind,
    ReaderStateValidationError,
    load_reader_state,
    set_reader_completion,
    set_want_to_read,
    update_reader_progress,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    ReaderOperation = Callable[[AsyncSession], Awaitable[object]]


@pytest.mark.asyncio
async def test_resume_moves_both_directions_without_clearing_completion(
    db_session: AsyncSession,
) -> None:
    user_id, issue_id = await _seed_user_and_issue(db_session)

    completed = await update_reader_progress(
        db_session,
        user_id=user_id,
        issue_id=issue_id,
        revision="revision-a",
        page_index=4,
        page_count=5,
        completion_candidate=True,
        expected_revision="revision-a",
        expected_page_count=5,
    )
    moved_back = await update_reader_progress(
        db_session,
        user_id=user_id,
        issue_id=issue_id,
        revision="revision-a",
        page_index=2,
        page_count=5,
        completion_candidate=False,
        expected_revision="revision-a",
        expected_page_count=5,
    )

    assert completed.after.completed_at is not None
    assert moved_back.after.last_page_index == 2
    assert moved_back.after.completed_at == completed.after.completed_at


@pytest.mark.asyncio
async def test_settled_reread_start_clears_completion_and_preserves_queue(
    db_session: AsyncSession,
) -> None:
    user_id, issue_id = await _seed_user_and_issue(db_session)
    completed = await update_reader_progress(
        db_session,
        user_id=user_id,
        issue_id=issue_id,
        revision="revision-a",
        page_index=4,
        page_count=5,
        completion_candidate=True,
        expected_revision="revision-a",
        expected_page_count=5,
    )
    queued = await set_want_to_read(
        db_session,
        user_id=user_id,
        issue_id=issue_id,
        enabled=True,
    )

    reread = await update_reader_progress(
        db_session,
        user_id=user_id,
        issue_id=issue_id,
        revision="revision-a",
        page_index=0,
        page_count=5,
        completion_candidate=False,
        expected_revision="revision-a",
        expected_page_count=5,
        reread_started=True,
    )

    assert reread.before == queued.after
    assert completed.after.completed_at is not None
    assert reread.after.completed_at is None
    assert reread.after.last_page_index == 0
    assert reread.after.is_continue_candidate is True
    assert reread.after.want_to_read is True
    assert reread.after.completion_updated_at is not None
    assert [event.kind for event in reread.events] == [ReaderStateEventKind.COMPLETION_CHANGED]
    assert reread.events[0].completed is False
    assert reread.events[0].origin is ReaderCompletionOrigin.REREAD


@pytest.mark.asyncio
async def test_reread_start_requires_page_one_without_completion(
    db_session: AsyncSession,
) -> None:
    user_id, issue_id = await _seed_user_and_issue(db_session)

    with pytest.raises(ReaderStateValidationError, match="Rereading must start on page one"):
        await update_reader_progress(
            db_session,
            user_id=user_id,
            issue_id=issue_id,
            revision="revision-a",
            page_index=1,
            page_count=5,
            completion_candidate=False,
            expected_revision="revision-a",
            expected_page_count=5,
            reread_started=True,
        )


@pytest.mark.asyncio
async def test_new_content_revision_preserves_completion_and_updates_progress_clocks(
    db_session: AsyncSession,
) -> None:
    user_id, issue_id = await _seed_user_and_issue(db_session)
    await update_reader_progress(
        db_session,
        user_id=user_id,
        issue_id=issue_id,
        revision="revision-a",
        page_index=4,
        page_count=5,
        completion_candidate=True,
        expected_revision="revision-a",
        expected_page_count=5,
    )

    updated = await update_reader_progress(
        db_session,
        user_id=user_id,
        issue_id=issue_id,
        revision="revision-b",
        page_index=0,
        page_count=6,
        completion_candidate=False,
        expected_revision="revision-b",
        expected_page_count=6,
    )

    assert updated.after.last_page_index == 0
    assert updated.after.content_revision == "revision-b"
    assert updated.after.completed_at is not None
    state = (
        await db_session.execute(
            select(IssueReaderState).where(
                IssueReaderState.user_id == user_id,
                IssueReaderState.issue_id == issue_id,
            )
        )
    ).scalar_one()
    assert state.progress_updated_at is not None
    assert state.last_opened_at == state.progress_updated_at
    assert state.completion_updated_at == state.completed_at
    assert state.state_version == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("revision", "page_index", "page_count", "completion_candidate"),
    [
        ("stale", 0, 5, False),
        ("revision-a", -1, 5, False),
        ("revision-a", 5, 5, False),
        ("revision-a", 0, 4, False),
        ("revision-a", 3, 5, True),
    ],
)
async def test_invalid_or_nonfinal_completion_updates_are_rejected(
    db_session: AsyncSession,
    revision: str,
    page_index: int,
    page_count: int,
    completion_candidate: bool,
) -> None:
    user_id, issue_id = await _seed_user_and_issue(db_session)

    with pytest.raises(ReaderStateValidationError):
        await update_reader_progress(
            db_session,
            user_id=user_id,
            issue_id=issue_id,
            revision=revision,
            page_index=page_index,
            page_count=page_count,
            completion_candidate=completion_candidate,
            expected_revision="revision-a",
            expected_page_count=5,
        )


@pytest.mark.asyncio
async def test_load_reader_state_is_private_to_user_and_issue(db_session: AsyncSession) -> None:
    user_id, issue_id = await _seed_user_and_issue(db_session)
    await update_reader_progress(
        db_session,
        user_id=user_id,
        issue_id=issue_id,
        revision="revision-a",
        page_index=2,
        page_count=5,
        completion_candidate=False,
        expected_revision="revision-a",
        expected_page_count=5,
    )

    own = await load_reader_state(db_session, user_id=user_id, issue_id=issue_id)
    absent = await load_reader_state(db_session, user_id=user_id + 1, issue_id=issue_id)

    assert own is not None
    assert own.last_page_index == 2
    assert absent is None


@pytest.mark.asyncio
async def test_reader_state_can_persist_queue_intent_without_progress(
    db_session: AsyncSession,
) -> None:
    user_id, issue_id = await _seed_user_and_issue(db_session)
    queued_at = datetime.now(UTC)
    state = IssueReaderState(
        user_id=user_id,
        issue_id=issue_id,
        want_to_read=True,
        want_to_read_updated_at=queued_at,
    )

    db_session.add(state)
    await db_session.flush()

    assert state.last_page_index is None
    assert state.content_revision is None
    assert state.page_count is None
    assert state.progress_updated_at is None
    assert state.last_opened_at is None
    assert state.completed_at is None
    assert state.completion_updated_at is None
    assert state.want_to_read is True
    assert state.want_to_read_updated_at == queued_at
    assert state.state_version == 1


def test_reader_state_model_exposes_nullable_progress_and_bounded_query_indexes() -> None:
    table = IssueReaderState.__table__

    assert table.c.last_page_index.nullable is True
    assert table.c.content_revision.nullable is True
    assert table.c.page_count.nullable is True
    assert table.c.want_to_read.nullable is False
    assert table.c.state_version.nullable is False
    assert {index.name for index in table.indexes} >= {
        "ix_issue_reader_states_user_last_opened",
        "ix_issue_reader_states_user_want_updated",
    }


@pytest.mark.asyncio
async def test_mark_read_clears_queue_and_repeated_mark_read_is_a_noop(
    db_session: AsyncSession,
) -> None:
    user_id, issue_id = await _seed_user_and_issue(db_session)
    queued_at = datetime.now(UTC)
    db_session.add(
        IssueReaderState(
            user_id=user_id,
            issue_id=issue_id,
            want_to_read=True,
            want_to_read_updated_at=queued_at,
        )
    )
    await db_session.flush()

    marked = await set_reader_completion(
        db_session,
        user_id=user_id,
        issue_id=issue_id,
        completed=True,
    )
    repeated = await set_reader_completion(
        db_session,
        user_id=user_id,
        issue_id=issue_id,
        completed=True,
    )

    assert marked.changed is True
    assert marked.after.has_progress is False
    assert marked.after.completed_at is not None
    assert marked.after.completion_updated_at == marked.after.completed_at
    assert marked.after.want_to_read is False
    assert marked.after.want_to_read_updated_at is not None
    assert marked.after.state_version == 2
    assert [event.kind for event in marked.events] == [
        ReaderStateEventKind.COMPLETION_CHANGED,
        ReaderStateEventKind.WANT_TO_READ_CHANGED,
    ]
    assert marked.events[0].origin is ReaderCompletionOrigin.MANUAL
    assert repeated.changed is False
    assert repeated.events == ()
    assert repeated.after.completed_at == marked.after.completed_at
    assert repeated.after.completion_updated_at == marked.after.completion_updated_at
    assert repeated.after.want_to_read_updated_at == marked.after.want_to_read_updated_at
    assert repeated.after.state_version == marked.after.state_version


@pytest.mark.asyncio
async def test_mark_read_without_existing_state_creates_completion_intent_only(
    db_session: AsyncSession,
) -> None:
    user_id, issue_id = await _seed_user_and_issue(db_session)

    marked = await set_reader_completion(
        db_session,
        user_id=user_id,
        issue_id=issue_id,
        completed=True,
    )

    assert marked.before is None
    assert marked.changed is True
    assert marked.after.has_progress is False
    assert marked.after.is_completed is True
    assert marked.after.want_to_read is False
    assert marked.after.want_to_read_updated_at is None
    assert marked.after.state_version == 1
    assert len(marked.events) == 1
    assert marked.events[0].kind is ReaderStateEventKind.COMPLETION_CHANGED


@pytest.mark.asyncio
async def test_mark_unread_preserves_progress_and_queue_and_is_idempotent(
    db_session: AsyncSession,
) -> None:
    user_id, issue_id = await _seed_user_and_issue(db_session)
    await update_reader_progress(
        db_session,
        user_id=user_id,
        issue_id=issue_id,
        revision="revision-a",
        page_index=4,
        page_count=5,
        completion_candidate=True,
        expected_revision="revision-a",
        expected_page_count=5,
    )
    state = (
        await db_session.execute(
            select(IssueReaderState).where(
                IssueReaderState.user_id == user_id,
                IssueReaderState.issue_id == issue_id,
            )
        )
    ).scalar_one()
    state.want_to_read = True
    state.want_to_read_updated_at = datetime.now(UTC)
    await db_session.flush()
    queue_clock = state.want_to_read_updated_at

    unread = await set_reader_completion(
        db_session,
        user_id=user_id,
        issue_id=issue_id,
        completed=False,
    )
    repeated = await set_reader_completion(
        db_session,
        user_id=user_id,
        issue_id=issue_id,
        completed=False,
    )

    assert unread.changed is True
    assert unread.after.last_page_index == 4
    assert unread.after.content_revision == "revision-a"
    assert unread.after.page_count == 5
    assert unread.after.completed_at is None
    assert unread.after.completion_updated_at is not None
    assert unread.after.is_explicitly_unread is True
    assert unread.after.is_continue_candidate is False
    assert unread.after.want_to_read is True
    assert unread.after.want_to_read_updated_at == queue_clock
    assert unread.events[0].kind is ReaderStateEventKind.COMPLETION_CHANGED
    assert unread.events[0].completed is False
    assert unread.events[0].origin is ReaderCompletionOrigin.MANUAL
    assert repeated.changed is False
    assert repeated.after.completion_updated_at == unread.after.completion_updated_at
    assert repeated.after.state_version == unread.after.state_version


@pytest.mark.asyncio
async def test_want_to_read_creates_intent_state_and_does_not_churn_or_touch_progress(
    db_session: AsyncSession,
) -> None:
    user_id, issue_id = await _seed_user_and_issue(db_session)

    queued = await set_want_to_read(
        db_session,
        user_id=user_id,
        issue_id=issue_id,
        enabled=True,
    )
    repeated = await set_want_to_read(
        db_session,
        user_id=user_id,
        issue_id=issue_id,
        enabled=True,
    )
    removed = await set_want_to_read(
        db_session,
        user_id=user_id,
        issue_id=issue_id,
        enabled=False,
    )

    assert queued.changed is True
    assert queued.after.has_progress is False
    assert queued.after.want_to_read is True
    assert queued.after.want_to_read_updated_at is not None
    assert queued.after.state_version == 1
    assert queued.events[0].kind is ReaderStateEventKind.WANT_TO_READ_CHANGED
    assert repeated.changed is False
    assert repeated.after.want_to_read_updated_at == queued.after.want_to_read_updated_at
    assert repeated.after.state_version == queued.after.state_version
    assert removed.changed is True
    assert removed.after.has_progress is False
    assert removed.after.want_to_read is False
    assert removed.after.want_to_read_updated_at is not None
    assert removed.after.want_to_read_updated_at != queued.after.want_to_read_updated_at
    assert removed.after.state_version == 2


@pytest.mark.asyncio
async def test_final_progress_completes_and_clears_active_queue_atomically(
    db_session: AsyncSession,
) -> None:
    user_id, issue_id = await _seed_user_and_issue(db_session)
    queued = await set_want_to_read(
        db_session,
        user_id=user_id,
        issue_id=issue_id,
        enabled=True,
    )

    completed = await update_reader_progress(
        db_session,
        user_id=user_id,
        issue_id=issue_id,
        revision="revision-a",
        page_index=4,
        page_count=5,
        completion_candidate=True,
        expected_revision="revision-a",
        expected_page_count=5,
    )

    assert completed.changed is True
    assert completed.before == queued.after
    assert completed.after.completed_at is not None
    assert completed.after.completion_updated_at == completed.after.completed_at
    assert completed.after.want_to_read is False
    assert completed.after.want_to_read_updated_at is not None
    assert completed.after.state_version == 2
    assert [event.kind for event in completed.events] == [
        ReaderStateEventKind.COMPLETION_CHANGED,
        ReaderStateEventKind.WANT_TO_READ_CHANGED,
    ]
    assert completed.events[0].completed is True
    assert completed.events[0].origin is ReaderCompletionOrigin.AUTOMATIC
    assert completed.events[1].want_to_read is False


@pytest.mark.asyncio
async def test_snapshot_derives_progress_completion_and_explicit_unread_state(
    db_session: AsyncSession,
) -> None:
    user_id, issue_id = await _seed_user_and_issue(db_session)
    progress = await update_reader_progress(
        db_session,
        user_id=user_id,
        issue_id=issue_id,
        revision="revision-a",
        page_index=1,
        page_count=5,
        completion_candidate=False,
        expected_revision="revision-a",
        expected_page_count=5,
    )

    assert progress.after.has_progress is True
    assert progress.after.is_completed is False
    assert progress.after.is_explicitly_unread is False
    assert progress.after.position_percent == 40
    assert progress.after.is_continue_candidate is True

    read = await set_reader_completion(
        db_session,
        user_id=user_id,
        issue_id=issue_id,
        completed=True,
    )
    unread = await set_reader_completion(
        db_session,
        user_id=user_id,
        issue_id=issue_id,
        completed=False,
    )

    assert read.after.is_completed is True
    assert read.after.is_continue_candidate is False
    assert unread.after.is_completed is False
    assert unread.after.is_explicitly_unread is True
    assert unread.after.position_percent == 40
    assert unread.after.is_continue_candidate is True


@pytest.mark.asyncio
async def test_concurrent_initial_progress_writes_create_one_state_row(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reader-state.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        user_id, issue_id = await _seed_user_and_issue(session)
        await session.commit()

    ready_count = 0
    both_initial_reads_complete = asyncio.Event()

    class CoordinatedSession:
        """Hold legacy read-before-insert writes until both observed no row."""

        def __init__(self, session: AsyncSession) -> None:
            self._session = session

        async def execute(self, statement, *args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal ready_count
            result = await self._session.execute(statement, *args, **kwargs)
            if isinstance(statement, Select):
                ready_count += 1
                if ready_count == 2:
                    both_initial_reads_complete.set()
                await asyncio.wait_for(both_initial_reads_complete.wait(), timeout=1)
            return result

        def __getattr__(self, name: str):
            return getattr(self._session, name)

    async def save(page_index: int):  # type: ignore[no-untyped-def]
        async with session_factory() as session:
            snapshot = await update_reader_progress(
                CoordinatedSession(session),  # type: ignore[arg-type]
                user_id=user_id,
                issue_id=issue_id,
                revision="revision-a",
                page_index=page_index,
                page_count=5,
                completion_candidate=False,
                expected_revision="revision-a",
                expected_page_count=5,
            )
            await session.commit()
            return snapshot

    try:
        snapshots = await asyncio.gather(save(1), save(2))
        async with session_factory() as session:
            rows = list((await session.execute(select(IssueReaderState))).scalars().all())
    finally:
        await engine.dispose()

    assert len(snapshots) == 2
    assert len(rows) == 1
    assert rows[0].last_page_index in {1, 2}


@pytest.mark.asyncio
async def test_concurrent_queue_add_and_initial_progress_preserve_both_dimensions(
    tmp_path,
) -> None:
    engine, factory, user_id, issue_id = await _reader_state_factory(tmp_path / "queue-progress.db")

    async def add_queue(session: AsyncSession) -> object:
        return await set_want_to_read(
            session,
            user_id=user_id,
            issue_id=issue_id,
            enabled=True,
        )

    async def save_progress(session: AsyncSession) -> object:
        return await update_reader_progress(
            session,
            user_id=user_id,
            issue_id=issue_id,
            revision="revision-a",
            page_index=1,
            page_count=5,
            completion_candidate=False,
            expected_revision="revision-a",
            expected_page_count=5,
        )

    try:
        await _run_reader_operations(factory, add_queue, save_progress)
        rows = await _reader_state_rows(factory)
    finally:
        await engine.dispose()

    assert len(rows) == 1
    assert rows[0].last_page_index == 1
    assert rows[0].want_to_read is True
    assert rows[0].state_version == 2


@pytest.mark.asyncio
async def test_concurrent_mark_read_and_progress_preserve_completion_and_position(
    tmp_path,
) -> None:
    engine, factory, user_id, issue_id = await _reader_state_factory(tmp_path / "read-progress.db")

    async def mark_read(session: AsyncSession) -> object:
        return await set_reader_completion(
            session,
            user_id=user_id,
            issue_id=issue_id,
            completed=True,
        )

    async def save_progress(session: AsyncSession) -> object:
        return await update_reader_progress(
            session,
            user_id=user_id,
            issue_id=issue_id,
            revision="revision-a",
            page_index=2,
            page_count=5,
            completion_candidate=False,
            expected_revision="revision-a",
            expected_page_count=5,
        )

    try:
        await _run_reader_operations(factory, mark_read, save_progress)
        rows = await _reader_state_rows(factory)
    finally:
        await engine.dispose()

    assert len(rows) == 1
    assert rows[0].last_page_index == 2
    assert rows[0].completed_at is not None
    assert rows[0].state_version == 2


@pytest.mark.asyncio
async def test_concurrent_mark_unread_and_progress_preserve_explicit_unread(
    tmp_path,
) -> None:
    engine, factory, user_id, issue_id = await _reader_state_factory(
        tmp_path / "unread-progress.db"
    )
    async with factory() as session:
        await update_reader_progress(
            session,
            user_id=user_id,
            issue_id=issue_id,
            revision="revision-a",
            page_index=4,
            page_count=5,
            completion_candidate=True,
            expected_revision="revision-a",
            expected_page_count=5,
        )
        await session.commit()

    async def mark_unread(session: AsyncSession) -> object:
        return await set_reader_completion(
            session,
            user_id=user_id,
            issue_id=issue_id,
            completed=False,
        )

    async def move_back(session: AsyncSession) -> object:
        return await update_reader_progress(
            session,
            user_id=user_id,
            issue_id=issue_id,
            revision="revision-a",
            page_index=2,
            page_count=5,
            completion_candidate=False,
            expected_revision="revision-a",
            expected_page_count=5,
        )

    try:
        await _run_reader_operations(factory, mark_unread, move_back)
        rows = await _reader_state_rows(factory)
    finally:
        await engine.dispose()

    assert len(rows) == 1
    assert rows[0].last_page_index == 2
    assert rows[0].completed_at is None
    assert rows[0].completion_updated_at is not None
    assert rows[0].state_version == 3


@pytest.mark.asyncio
async def test_concurrent_duplicate_queue_adds_are_idempotent(tmp_path) -> None:
    engine, factory, user_id, issue_id = await _reader_state_factory(
        tmp_path / "duplicate-queue.db"
    )

    async def add_queue(session: AsyncSession) -> object:
        return await set_want_to_read(
            session,
            user_id=user_id,
            issue_id=issue_id,
            enabled=True,
        )

    try:
        await _run_reader_operations(factory, add_queue, add_queue)
        rows = await _reader_state_rows(factory)
    finally:
        await engine.dispose()

    assert len(rows) == 1
    assert rows[0].want_to_read is True
    assert rows[0].state_version == 1


@pytest.mark.asyncio
async def test_concurrent_final_progress_and_queue_add_preserve_completion(
    tmp_path,
) -> None:
    engine, factory, user_id, issue_id = await _reader_state_factory(tmp_path / "final-queue.db")

    async def complete(session: AsyncSession) -> object:
        return await update_reader_progress(
            session,
            user_id=user_id,
            issue_id=issue_id,
            revision="revision-a",
            page_index=4,
            page_count=5,
            completion_candidate=True,
            expected_revision="revision-a",
            expected_page_count=5,
        )

    async def add_queue(session: AsyncSession) -> object:
        return await set_want_to_read(
            session,
            user_id=user_id,
            issue_id=issue_id,
            enabled=True,
        )

    try:
        await _run_reader_operations(factory, complete, add_queue)
        rows = await _reader_state_rows(factory)
    finally:
        await engine.dispose()

    assert len(rows) == 1
    assert rows[0].last_page_index == 4
    assert rows[0].completed_at is not None
    assert rows[0].state_version == 2


async def _seed_user_and_issue(session: AsyncSession) -> tuple[int, int]:
    from pullbox.models.issue import Issue, IssueStatus
    from pullbox.models.series import Series, SeriesStatus, SeriesType
    from pullbox.models.user import User
    from pullbox.services.auth_service import AuthService

    user = User(username="reader", password_hash=AuthService.hash_password("Test@1234"))
    series = Series(
        comicvine_id=None,
        title="Reader State Series",
        sort_title="reader state series",
        year_start=2026,
        status=SeriesStatus.CONTINUING,
        series_type=SeriesType.STANDARD,
        monitored=True,
        issue_count=1,
    )
    session.add_all([user, series])
    await session.flush()
    issue = Issue(
        series_id=series.id,
        issue_number=1,
        title="Reader State Issue",
        status=IssueStatus.OWNED,
    )
    session.add(issue)
    await session.flush()
    assert isinstance(user.created_at, datetime)
    return user.id, issue.id


async def _reader_state_factory(db_path):  # type: ignore[no-untyped-def]
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        user_id, issue_id = await _seed_user_and_issue(session)
        await session.commit()
    return engine, factory, user_id, issue_id


async def _run_reader_operations(
    factory: async_sessionmaker[AsyncSession],
    *operations: ReaderOperation,
) -> None:
    async def run(operation: ReaderOperation) -> None:
        async with factory() as session:
            await operation(session)
            await session.commit()

    await asyncio.gather(*(run(operation) for operation in operations))


async def _reader_state_rows(
    factory: async_sessionmaker[AsyncSession],
) -> list[IssueReaderState]:
    async with factory() as session:
        return list((await session.execute(select(IssueReaderState))).scalars().all())
