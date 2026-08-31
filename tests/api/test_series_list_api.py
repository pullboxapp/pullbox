"""Tests for GET /api/v1/series list query behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from pullbox.api.v1.series import get_series, list_series, list_series_issues
from pullbox.models import Base
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.publisher import Publisher
from pullbox.models.series import Series, SeriesStatus
from pullbox.models.user import User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


class SelectRecorder:
    """Capture SELECT statements emitted during a focused route call."""

    def __init__(self, async_engine: AsyncEngine) -> None:
        self._engine = async_engine.sync_engine
        self.statements: list[str] = []

    def __enter__(self) -> SelectRecorder:
        event.listen(self._engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc_info: object) -> None:
        event.remove(self._engine, "before_cursor_execute", self._record)

    def _record(
        self,
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            self.statements.append(statement)


@pytest.mark.asyncio
async def test_list_series_counts_issues_without_eager_loading_issue_rows() -> None:
    """List responses keep count parity without selecting every issue row."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            publisher = Publisher(name="DC Comics")
            session.add(publisher)
            await session.flush()

            batman = Series(
                comicvine_id=1001,
                title="Batman",
                sort_title="Batman",
                year_start=2016,
                status=SeriesStatus.CONTINUING,
                publisher_id=publisher.id,
                monitored=True,
                issue_count=3,
            )
            superman = Series(
                comicvine_id=1002,
                title="Superman",
                sort_title="Superman",
                year_start=2025,
                status=SeriesStatus.CONTINUING,
                publisher_id=publisher.id,
                monitored=False,
                issue_count=2,
            )
            session.add_all([batman, superman])
            await session.flush()
            session.add_all(
                [
                    Issue(series_id=batman.id, issue_number=1, status=IssueStatus.OWNED),
                    Issue(series_id=batman.id, issue_number=2, status=IssueStatus.WANTED),
                    Issue(series_id=batman.id, issue_number=3, status=IssueStatus.SKIPPED),
                    Issue(series_id=superman.id, issue_number=1, status=IssueStatus.WANTED),
                    Issue(series_id=superman.id, issue_number=2, status=IssueStatus.WANTED),
                ]
            )
            await session.commit()

        async with factory() as session:
            with SelectRecorder(engine) as recorder:
                response = await list_series(
                    User(username="apiuser", password_hash="test"),
                    session,
                    limit=50,
                    offset=0,
                    publisher_id=None,
                    status=None,
                    monitored=None,
                    year=None,
                    sort="title",
                    order="asc",
                )

        by_title = {item.title: item for item in response.items}
        assert response.total == 2
        assert by_title["Batman"].owned_count == 1
        assert by_title["Batman"].wanted_count == 1
        assert by_title["Superman"].owned_count == 0
        assert by_title["Superman"].wanted_count == 2
        assert all("issues_1" not in statement for statement in recorder.statements)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_get_series_counts_issues_without_eager_loading_issue_rows() -> None:
    """Detail responses keep count parity without selecting every issue row."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            publisher = Publisher(name="DC Comics")
            session.add(publisher)
            await session.flush()

            series = Series(
                comicvine_id=1001,
                title="Batman",
                sort_title="Batman",
                year_start=2016,
                status=SeriesStatus.CONTINUING,
                publisher_id=publisher.id,
                monitored=True,
                issue_count=3,
            )
            session.add(series)
            await session.flush()
            session.add_all(
                [
                    Issue(series_id=series.id, issue_number=1, status=IssueStatus.OWNED),
                    Issue(series_id=series.id, issue_number=2, status=IssueStatus.WANTED),
                    Issue(series_id=series.id, issue_number=3, status=IssueStatus.SKIPPED),
                ]
            )
            series_id = series.id
            await session.commit()

        async with factory() as session:
            with SelectRecorder(engine) as recorder:
                response = await get_series(
                    series_id,
                    User(username="apiuser", password_hash="test"),
                    session,
                )

        assert response.title == "Batman"
        assert response.owned_count == 1
        assert response.wanted_count == 1
        assert all("issues_1" not in statement for statement in recorder.statements)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_series_issues_adds_exact_text_with_legacy_fallback() -> None:
    """Issue-list responses add exact text without changing the numeric field."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            series = Series(
                title="DC One Million",
                sort_title="DC One Million",
                status=SeriesStatus.ENDED,
                issue_count=1,
            )
            session.add(series)
            await session.flush()
            issue = Issue(
                series_id=series.id,
                issue_number=1_000_000.0,
                status=IssueStatus.OWNED,
            )
            session.add(issue)
            await session.flush()
            issue.issue_number_text = None
            await session.commit()
            series_id = series.id

        async with factory() as session:
            response = await list_series_issues(
                series_id,
                User(username="apiuser", password_hash="test"),
                session,
                limit=100,
                offset=0,
            )

        assert response.items[0].issue_number == 1_000_000.0
        assert response.items[0].issue_number_text == "1000000"
    finally:
        await engine.dispose()
