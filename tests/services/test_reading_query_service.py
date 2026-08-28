"""Bounded private reading projection tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event, select

from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.reader import IssueReaderState
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.models.user import User
from pullbox.services.auth_service import AuthService
from pullbox.services.reader_state_service import set_want_to_read, update_reader_progress
from pullbox.services.reading_query_service import (
    list_continue_reading,
    list_read_issues,
    list_want_to_read,
    load_adjacent_readable_issues,
    load_reader_issue_access,
    load_series_reading_aggregates,
    load_visible_issue_states,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


@pytest.fixture
async def reading_catalog(db_session: AsyncSession) -> AsyncIterator[dict[str, object]]:
    now = datetime.now(UTC)
    user = User(username="reader-one", password_hash=AuthService.hash_password("Test@1234"))
    other_user = User(
        username="reader-two",
        password_hash=AuthService.hash_password("Test@1234"),
    )
    root = LibraryRoot(name="Library", path="/comics", enabled=True)
    series = Series(
        title="Reading Series",
        sort_title="reading series",
        year_start=2026,
        status=SeriesStatus.CONTINUING,
        series_type=SeriesType.STANDARD,
        monitored=True,
        issue_count=8,
        cover_url="https://example.test/series.jpg",
        library_root=root,
    )
    other_series = Series(
        title="Other Series",
        sort_title="other series",
        year_start=2025,
        status=SeriesStatus.CONTINUING,
        series_type=SeriesType.STANDARD,
        monitored=True,
        issue_count=1,
        library_root=root,
    )
    db_session.add_all([user, other_user, root, series, other_series])
    await db_session.flush()

    issues: list[Issue] = []
    formats = [
        FileFormat.CBZ,
        FileFormat.CBR,
        FileFormat.PDF,
        FileFormat.EPUB,
        FileFormat.CBZ,
        None,
        FileFormat.CBZ,
        FileFormat.CBZ,
    ]
    for index, file_format in enumerate(formats, start=1):
        issue = Issue(
            series_id=series.id,
            issue_number=float(index),
            title=f"Issue {index}",
            status=IssueStatus.OWNED if index != 7 else IssueStatus.WANTED,
            cover_url=f"https://example.test/{index}.jpg",
        )
        db_session.add(issue)
        await db_session.flush()
        issues.append(issue)
        if file_format is not None:
            db_session.add(
                LibraryFile(
                    file_path=f"/comics/issue-{index}.{file_format.value}",
                    file_name=f"issue-{index}.{file_format.value}",
                    file_size=1024,
                    file_format=file_format,
                    file_modified_at=now,
                    match_confidence=MatchConfidence.HIGH,
                    issue_id=issue.id,
                    library_root_id=root.id,
                )
            )

    other_issue = Issue(
        series_id=other_series.id,
        issue_number=1,
        title="Other Issue",
        status=IssueStatus.OWNED,
    )
    db_session.add(other_issue)
    await db_session.flush()
    db_session.add(
        LibraryFile(
            file_path="/comics/other.cbz",
            file_name="other.cbz",
            file_size=1024,
            file_format=FileFormat.CBZ,
            file_modified_at=now,
            match_confidence=MatchConfidence.HIGH,
            issue_id=other_issue.id,
            library_root_id=root.id,
        )
    )

    states = [
        IssueReaderState(
            user_id=user.id,
            issue_id=issues[0].id,
            last_page_index=0,
            content_revision="r1",
            page_count=5,
            progress_updated_at=now - timedelta(minutes=3),
            last_opened_at=now - timedelta(minutes=3),
        ),
        IssueReaderState(
            user_id=user.id,
            issue_id=issues[1].id,
            last_page_index=2,
            content_revision="r2",
            page_count=5,
            progress_updated_at=now - timedelta(minutes=1),
            last_opened_at=now - timedelta(minutes=1),
            want_to_read=True,
            want_to_read_updated_at=now - timedelta(minutes=4),
        ),
        IssueReaderState(
            user_id=user.id,
            issue_id=issues[2].id,
            last_page_index=4,
            content_revision="r3",
            page_count=5,
            progress_updated_at=now - timedelta(minutes=2),
            last_opened_at=now - timedelta(minutes=2),
            completed_at=now - timedelta(minutes=2),
            completion_updated_at=now - timedelta(minutes=2),
        ),
        IssueReaderState(
            user_id=user.id,
            issue_id=issues[3].id,
            want_to_read=True,
            want_to_read_updated_at=now - timedelta(minutes=2),
        ),
        IssueReaderState(
            user_id=user.id,
            issue_id=issues[4].id,
            last_page_index=4,
            content_revision="r5",
            page_count=5,
            progress_updated_at=now,
            last_opened_at=now,
            completion_updated_at=now,
        ),
        IssueReaderState(
            user_id=user.id,
            issue_id=issues[5].id,
            want_to_read=True,
            want_to_read_updated_at=now - timedelta(minutes=1),
        ),
        IssueReaderState(
            user_id=user.id,
            issue_id=issues[6].id,
            last_page_index=1,
            content_revision="r7",
            page_count=5,
            progress_updated_at=now,
            last_opened_at=now,
        ),
        IssueReaderState(
            user_id=other_user.id,
            issue_id=issues[7].id,
            last_page_index=1,
            content_revision="other",
            page_count=5,
            progress_updated_at=now + timedelta(minutes=1),
            last_opened_at=now + timedelta(minutes=1),
            completed_at=now + timedelta(minutes=1),
            completion_updated_at=now + timedelta(minutes=1),
            want_to_read=True,
            want_to_read_updated_at=now + timedelta(minutes=1),
        ),
    ]
    db_session.add_all(states)
    await db_session.flush()
    yield {
        "user": user,
        "other_user": other_user,
        "series": series,
        "other_series": other_series,
        "issues": issues,
        "other_issue": other_issue,
    }


@pytest.mark.asyncio
async def test_continue_is_readable_incomplete_bounded_and_user_private(
    async_engine: AsyncEngine,
    db_session: AsyncSession,
    reading_catalog: dict[str, object],
) -> None:
    user = reading_catalog["user"]
    issues = reading_catalog["issues"]
    assert isinstance(user, User)
    assert isinstance(issues, list)

    statements: list[tuple[str, object]] = []

    def record_statement(*args: object) -> None:
        statements.append((str(args[2]), args[3]))

    event.listen(async_engine.sync_engine, "before_cursor_execute", record_statement)
    try:
        result = await list_continue_reading(db_session, user_id=user.id, page=1, per_page=1)
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", record_statement)

    assert len(statements) == 2
    connection = await db_session.connection()
    query_plan = (
        await connection.exec_driver_sql(
            f"EXPLAIN QUERY PLAN {statements[1][0]}",
            statements[1][1],
        )
    ).all()
    assert any(
        "ix_issue_reader_states_user_last_opened" in str(plan_row) for plan_row in query_plan
    )
    assert result.total == 2
    assert result.page == 1
    assert result.per_page == 1
    assert [item.issue_id for item in result.items] == [issues[1].id]
    assert result.items[0].readable is True
    assert result.items[0].state.position_percent == 60
    assert not hasattr(result.items[0].state, "content_revision")
    assert not hasattr(result.items[0], "file_path")
    second = await list_continue_reading(db_session, user_id=user.id, page=2, per_page=1)
    assert [item.issue_id for item in second.items] == [issues[0].id]


@pytest.mark.asyncio
async def test_settled_reread_moves_read_issue_to_continue_and_preserves_want(
    db_session: AsyncSession,
    reading_catalog: dict[str, object],
) -> None:
    user = reading_catalog["user"]
    issues = reading_catalog["issues"]
    assert isinstance(user, User)
    assert isinstance(issues, list)
    issue = issues[2]
    assert isinstance(issue, Issue)
    await set_want_to_read(
        db_session,
        user_id=user.id,
        issue_id=issue.id,
        enabled=True,
    )

    await update_reader_progress(
        db_session,
        user_id=user.id,
        issue_id=issue.id,
        revision="r3",
        page_index=0,
        page_count=5,
        completion_candidate=False,
        expected_revision="r3",
        expected_page_count=5,
        reread_started=True,
    )

    continued = await list_continue_reading(db_session, user_id=user.id, page=1, per_page=24)
    wanted = await list_want_to_read(db_session, user_id=user.id, page=1, per_page=24)
    read = await list_read_issues(db_session, user_id=user.id, page=1, per_page=24)

    assert issue.id in {item.issue_id for item in continued.items}
    assert issue.id in {item.issue_id for item in wanted.items}
    assert issue.id not in {item.issue_id for item in read.items}


@pytest.mark.asyncio
async def test_reading_lists_deduplicate_issues_with_multiple_registered_files(
    db_session: AsyncSession,
    reading_catalog: dict[str, object],
) -> None:
    user = reading_catalog["user"]
    issues = reading_catalog["issues"]
    series = reading_catalog["series"]
    assert isinstance(user, User)
    assert isinstance(issues, list)
    assert isinstance(series, Series)
    original_file = (
        await db_session.execute(select(LibraryFile).where(LibraryFile.issue_id == issues[1].id))
    ).scalar_one()
    db_session.add(
        LibraryFile(
            file_path="/comics/issue-2-duplicate.pdf",
            file_name="issue-2-duplicate.pdf",
            file_size=2048,
            file_format=FileFormat.PDF,
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.HIGH,
            issue_id=issues[1].id,
            library_root_id=original_file.library_root_id,
        )
    )
    await db_session.flush()

    continued = await list_continue_reading(db_session, user_id=user.id, page=1, per_page=24)
    wanted = await list_want_to_read(db_session, user_id=user.id, page=1, per_page=24)

    assert continued.total == 2
    assert [item.issue_id for item in continued.items].count(issues[1].id) == 1
    assert wanted.total == 3
    assert [item.issue_id for item in wanted.items].count(issues[1].id) == 1


@pytest.mark.asyncio
async def test_want_and_read_keep_unavailable_items_and_stable_order(
    db_session: AsyncSession,
    reading_catalog: dict[str, object],
) -> None:
    user = reading_catalog["user"]
    issues = reading_catalog["issues"]
    assert isinstance(user, User)
    assert isinstance(issues, list)

    wanted = await list_want_to_read(db_session, user_id=user.id, page=1, per_page=24)
    read = await list_read_issues(db_session, user_id=user.id, page=1, per_page=24)

    assert wanted.total == 3
    assert [item.issue_id for item in wanted.items] == [
        issues[5].id,
        issues[3].id,
        issues[1].id,
    ]
    assert [item.readable for item in wanted.items] == [False, False, True]
    assert read.total == 1
    assert [item.issue_id for item in read.items] == [issues[2].id]
    assert issues[7].id not in {item.issue_id for item in (*wanted.items, *read.items)}
    assert all(not hasattr(item.state, "user_id") for item in (*wanted.items, *read.items))


@pytest.mark.asyncio
async def test_reading_order_uses_issue_id_for_equal_state_timestamps(
    db_session: AsyncSession,
    reading_catalog: dict[str, object],
) -> None:
    user = reading_catalog["user"]
    issues = reading_catalog["issues"]
    assert isinstance(user, User)
    assert isinstance(issues, list)
    tied_at = datetime.now(UTC)
    states = list(
        (
            await db_session.execute(
                select(IssueReaderState).where(
                    IssueReaderState.user_id == user.id,
                    IssueReaderState.want_to_read.is_(True),
                )
            )
        ).scalars()
    )
    for state in states:
        state.want_to_read_updated_at = tied_at
    db_session.add(
        IssueReaderState(
            user_id=user.id,
            issue_id=issues[7].id,
            completed_at=tied_at,
            completion_updated_at=tied_at,
        )
    )
    completed = (
        await db_session.execute(
            select(IssueReaderState).where(
                IssueReaderState.user_id == user.id,
                IssueReaderState.issue_id == issues[2].id,
            )
        )
    ).scalar_one()
    completed.completed_at = tied_at
    completed.completion_updated_at = tied_at
    await db_session.flush()

    wanted = await list_want_to_read(db_session, user_id=user.id, page=1, per_page=24)
    read = await list_read_issues(db_session, user_id=user.id, page=1, per_page=24)

    assert [item.issue_id for item in wanted.items] == sorted(
        (issues[1].id, issues[3].id, issues[5].id),
        reverse=True,
    )
    assert [item.issue_id for item in read.items] == sorted(
        (issues[2].id, issues[7].id),
        reverse=True,
    )


@pytest.mark.asyncio
async def test_continue_excludes_incomplete_progress_triples(
    db_session: AsyncSession,
    reading_catalog: dict[str, object],
) -> None:
    user = reading_catalog["user"]
    issues = reading_catalog["issues"]
    assert isinstance(user, User)
    assert isinstance(issues, list)
    state = (
        await db_session.execute(
            select(IssueReaderState).where(
                IssueReaderState.user_id == user.id,
                IssueReaderState.issue_id == issues[0].id,
            )
        )
    ).scalar_one()
    state.content_revision = None
    await db_session.flush()

    result = await list_continue_reading(db_session, user_id=user.id, page=1, per_page=24)

    assert result.total == 1
    assert [item.issue_id for item in result.items] == [issues[1].id]


@pytest.mark.asyncio
async def test_reading_lists_reject_unbounded_inputs(db_session: AsyncSession) -> None:
    for page, per_page in ((0, 24), (1, 0), (1, 101)):
        with pytest.raises(ValueError):
            await list_continue_reading(
                db_session,
                user_id=1,
                page=page,
                per_page=per_page,
            )


@pytest.mark.asyncio
async def test_visible_issue_state_map_is_one_query_private_and_empty_fast_path(
    async_engine: AsyncEngine,
    db_session: AsyncSession,
    reading_catalog: dict[str, object],
) -> None:
    user = reading_catalog["user"]
    issues = reading_catalog["issues"]
    assert isinstance(user, User)
    assert isinstance(issues, list)
    statements: list[str] = []

    def record_statement(*args: object) -> None:
        statements.append(str(args[2]))

    event.listen(async_engine.sync_engine, "before_cursor_execute", record_statement)
    try:
        empty = await load_visible_issue_states(
            db_session,
            user_id=user.id,
            issue_ids=(),
        )
        assert empty == {}
        assert statements == []
        states = await load_visible_issue_states(
            db_session,
            user_id=user.id,
            issue_ids=(issues[0].id, issues[1].id, issues[7].id),
        )
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", record_statement)

    assert len(statements) == 1
    assert set(states) == {issues[0].id, issues[1].id}
    with pytest.raises(ValueError):
        await load_visible_issue_states(
            db_session,
            user_id=user.id,
            issue_ids=tuple(range(51)),
        )


@pytest.mark.asyncio
async def test_series_aggregates_count_only_readable_owned_issues_in_one_query(
    async_engine: AsyncEngine,
    db_session: AsyncSession,
    reading_catalog: dict[str, object],
) -> None:
    user = reading_catalog["user"]
    series = reading_catalog["series"]
    other_series = reading_catalog["other_series"]
    assert isinstance(user, User)
    assert isinstance(series, Series)
    assert isinstance(other_series, Series)
    statements: list[str] = []

    def record_statement(*args: object) -> None:
        statements.append(str(args[2]))

    event.listen(async_engine.sync_engine, "before_cursor_execute", record_statement)
    try:
        empty = await load_series_reading_aggregates(
            db_session,
            user_id=user.id,
            series_ids=(),
        )
        assert empty == {}
        assert statements == []
        aggregates = await load_series_reading_aggregates(
            db_session,
            user_id=user.id,
            series_ids=(series.id, other_series.id),
        )
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", record_statement)

    assert len(statements) == 1
    assert aggregates[series.id].readable_count == 5
    assert aggregates[series.id].completed_count == 1
    assert aggregates[series.id].in_progress_count == 2
    assert aggregates[series.id].completion_percent == 20
    assert aggregates[other_series.id].readable_count == 1
    assert aggregates[other_series.id].completed_count == 0
    with pytest.raises(ValueError):
        await load_series_reading_aggregates(
            db_session,
            user_id=user.id,
            series_ids=tuple(range(501)),
        )


@pytest.mark.asyncio
async def test_series_aggregate_bound_matches_the_existing_registry_page_contract(
    db_session: AsyncSession,
) -> None:
    aggregates = await load_series_reading_aggregates(
        db_session,
        user_id=1,
        series_ids=tuple(range(500)),
    )

    assert aggregates == {}


@pytest.mark.asyncio
async def test_adjacency_skips_unreadable_missing_and_unowned_issues(
    db_session: AsyncSession,
    reading_catalog: dict[str, object],
) -> None:
    series = reading_catalog["series"]
    issues = reading_catalog["issues"]
    assert isinstance(series, Series)
    assert isinstance(issues, list)

    around_six = await load_adjacent_readable_issues(
        db_session,
        series_id=series.id,
        current_issue_number=6,
        current_issue_id=issues[5].id,
    )

    assert around_six.previous is not None
    assert around_six.previous.issue_id == issues[4].id
    assert around_six.next is not None
    assert around_six.next.issue_id == issues[7].id
    assert not hasattr(around_six.next, "manifest_url")
    assert not hasattr(around_six.next, "file_path")


@pytest.mark.asyncio
async def test_reader_issue_access_distinguishes_supported_registered_files(
    db_session: AsyncSession,
    reading_catalog: dict[str, object],
) -> None:
    issues = reading_catalog["issues"]
    assert isinstance(issues, list)

    readable = await load_reader_issue_access(db_session, issue_id=issues[2].id)
    unsupported = await load_reader_issue_access(db_session, issue_id=issues[3].id)
    unowned = await load_reader_issue_access(db_session, issue_id=issues[6].id)
    missing = await load_reader_issue_access(db_session, issue_id=999999)

    assert readable is not None and readable.readable is True
    assert unsupported is not None and unsupported.readable is False
    assert unowned is not None and unowned.readable is False
    assert missing is None
