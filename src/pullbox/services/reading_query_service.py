"""Bounded, adapter-neutral projections for private reading workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import and_, case, func, or_, select

from pullbox.core.page_sources import SUPPORTED_READER_FORMATS
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile
from pullbox.models.reader import IssueReaderState
from pullbox.models.series import Series
from pullbox.services.reader_state_service import ReaderStateSnapshot, snapshot_reader_state

if TYPE_CHECKING:
    from datetime import datetime
    from typing import Any

    from sqlalchemy.engine import Row
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import Select
    from sqlalchemy.sql.elements import ColumnElement


@dataclass(frozen=True, slots=True)
class ReadingStateProjection:
    """Path- and revision-free reader state safe for presentation adapters."""

    last_page_index: int | None
    page_count: int | None
    progress_updated_at: datetime | None
    last_opened_at: datetime | None
    completed_at: datetime | None
    completion_updated_at: datetime | None
    want_to_read: bool
    want_to_read_updated_at: datetime | None
    state_version: int

    @property
    def has_progress(self) -> bool:
        return self.last_page_index is not None and self.page_count is not None

    @property
    def is_completed(self) -> bool:
        return self.completed_at is not None

    @property
    def is_explicitly_unread(self) -> bool:
        return self.completed_at is None and self.completion_updated_at is not None

    @property
    def position_percent(self) -> int:
        if self.last_page_index is None or self.page_count is None:
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
class ReadingIssueRecord:
    """Issue, series, readability, and private state facts for an adapter."""

    issue_id: int
    issue_number: float
    issue_title: str | None
    issue_cover_path: str | None
    issue_cover_url: str | None
    series_id: int
    series_title: str
    series_year: int | None
    series_cover_path: str | None
    series_cover_url: str | None
    readable: bool
    file_format: FileFormat | None
    state: ReadingStateProjection


@dataclass(frozen=True, slots=True)
class ReadingPage:
    """One deterministic bounded page of reading records."""

    items: tuple[ReadingIssueRecord, ...]
    total: int
    page: int
    per_page: int

    @property
    def page_count(self) -> int:
        if self.total <= 0:
            return 0
        return (self.total + self.per_page - 1) // self.per_page


@dataclass(frozen=True, slots=True)
class SeriesReadingAggregate:
    """Readable and completed issue totals for one visible series."""

    series_id: int
    readable_count: int
    completed_count: int
    in_progress_count: int

    @property
    def completion_percent(self) -> int:
        if self.readable_count <= 0:
            return 0
        return (self.completed_count * 100) // self.readable_count


@dataclass(frozen=True, slots=True)
class AdjacentIssueReference:
    """Path-free identity for a readable issue beside the active issue."""

    issue_id: int
    issue_number: float
    title: str | None


@dataclass(frozen=True, slots=True)
class AdjacentIssues:
    """Previous and next readable issues within one series."""

    previous: AdjacentIssueReference | None
    next: AdjacentIssueReference | None


@dataclass(frozen=True, slots=True)
class ReaderIssueAccess:
    """Path-free catalog existence and readability authorization facts."""

    issue_id: int
    readable: bool


async def list_continue_reading(
    session: AsyncSession,
    *,
    user_id: int,
    page: int,
    per_page: int,
) -> ReadingPage:
    _validate_page(page=page, per_page=per_page)
    filters = (
        IssueReaderState.user_id == user_id,
        IssueReaderState.last_opened_at.is_not(None),
        IssueReaderState.last_page_index.is_not(None),
        IssueReaderState.content_revision.is_not(None),
        IssueReaderState.page_count.is_not(None),
        IssueReaderState.last_page_index >= 0,
        IssueReaderState.page_count > 0,
        IssueReaderState.last_page_index < IssueReaderState.page_count - 1,
        IssueReaderState.completed_at.is_(None),
        Issue.status == IssueStatus.OWNED,
    )
    return await _list_reading_issues(
        session,
        filters=filters,
        order_by=(
            IssueReaderState.last_opened_at.desc(),
            IssueReaderState.issue_id.desc(),
        ),
        page=page,
        per_page=per_page,
        require_file=True,
    )


async def list_want_to_read(
    session: AsyncSession,
    *,
    user_id: int,
    page: int,
    per_page: int,
) -> ReadingPage:
    _validate_page(page=page, per_page=per_page)
    return await _list_reading_issues(
        session,
        filters=(
            IssueReaderState.user_id == user_id,
            IssueReaderState.want_to_read.is_(True),
        ),
        order_by=(
            IssueReaderState.want_to_read_updated_at.desc(),
            IssueReaderState.issue_id.desc(),
        ),
        page=page,
        per_page=per_page,
        require_file=False,
    )


async def list_read_issues(
    session: AsyncSession,
    *,
    user_id: int,
    page: int,
    per_page: int,
) -> ReadingPage:
    _validate_page(page=page, per_page=per_page)
    return await _list_reading_issues(
        session,
        filters=(
            IssueReaderState.user_id == user_id,
            IssueReaderState.completed_at.is_not(None),
        ),
        order_by=(
            IssueReaderState.completed_at.desc(),
            IssueReaderState.issue_id.desc(),
        ),
        page=page,
        per_page=per_page,
        require_file=False,
    )


async def load_visible_issue_states(
    session: AsyncSession,
    *,
    user_id: int,
    issue_ids: tuple[int, ...],
) -> dict[int, ReadingStateProjection]:
    if not issue_ids:
        return {}
    if len(issue_ids) > 50:
        raise ValueError("Visible issue state queries are limited to 50 issues.")
    result = await session.execute(
        select(IssueReaderState).where(
            IssueReaderState.user_id == user_id,
            IssueReaderState.issue_id.in_(issue_ids),
        )
    )
    return {state.issue_id: _state_projection(state) for state in result.scalars()}


async def load_series_reading_aggregates(
    session: AsyncSession,
    *,
    user_id: int,
    series_ids: tuple[int, ...],
) -> dict[int, SeriesReadingAggregate]:
    if not series_ids:
        return {}
    if len(series_ids) > 500:
        raise ValueError("Visible series aggregate queries are limited to 500 series.")

    completed_issue = case((IssueReaderState.completed_at.is_not(None), Issue.id))
    in_progress_issue = case(
        (
            and_(
                IssueReaderState.last_page_index.is_not(None),
                IssueReaderState.content_revision.is_not(None),
                IssueReaderState.page_count.is_not(None),
                IssueReaderState.last_page_index >= 0,
                IssueReaderState.page_count > 0,
                IssueReaderState.completed_at.is_(None),
                IssueReaderState.last_page_index < IssueReaderState.page_count - 1,
            ),
            Issue.id,
        )
    )
    result = await session.execute(
        select(
            Issue.series_id,
            func.count(func.distinct(Issue.id)),
            func.count(func.distinct(completed_issue)),
            func.count(func.distinct(in_progress_issue)),
        )
        .join(
            LibraryFile,
            and_(
                LibraryFile.issue_id == Issue.id,
                LibraryFile.file_format.in_(SUPPORTED_READER_FORMATS),
            ),
        )
        .outerjoin(
            IssueReaderState,
            and_(
                IssueReaderState.issue_id == Issue.id,
                IssueReaderState.user_id == user_id,
            ),
        )
        .where(
            Issue.series_id.in_(series_ids),
            Issue.status == IssueStatus.OWNED,
        )
        .group_by(Issue.series_id)
    )
    return {
        int(series_id): SeriesReadingAggregate(
            series_id=int(series_id),
            readable_count=int(readable_count),
            completed_count=int(completed_count),
            in_progress_count=int(in_progress_count),
        )
        for series_id, readable_count, completed_count, in_progress_count in result.all()
    }


async def load_adjacent_readable_issues(
    session: AsyncSession,
    *,
    series_id: int,
    current_issue_number: float,
    current_issue_id: int,
) -> AdjacentIssues:
    previous_result = await session.execute(
        _adjacent_statement(
            series_id=series_id,
            boundary=or_(
                Issue.issue_number < current_issue_number,
                and_(
                    Issue.issue_number == current_issue_number,
                    Issue.id < current_issue_id,
                ),
            ),
            descending=True,
        )
    )
    next_result = await session.execute(
        _adjacent_statement(
            series_id=series_id,
            boundary=or_(
                Issue.issue_number > current_issue_number,
                and_(
                    Issue.issue_number == current_issue_number,
                    Issue.id > current_issue_id,
                ),
            ),
            descending=False,
        )
    )
    return AdjacentIssues(
        previous=_adjacent_reference(previous_result.one_or_none()),
        next=_adjacent_reference(next_result.one_or_none()),
    )


async def load_reader_issue_access(
    session: AsyncSession,
    *,
    issue_id: int,
) -> ReaderIssueAccess | None:
    """Load existence and registered-format readability in one bounded query."""
    readable_file = case(
        (
            and_(
                Issue.status == IssueStatus.OWNED,
                LibraryFile.file_format.in_(SUPPORTED_READER_FORMATS),
            ),
            LibraryFile.id,
        )
    )
    row = (
        await session.execute(
            select(
                Issue.id,
                func.count(readable_file),
            )
            .outerjoin(LibraryFile, LibraryFile.issue_id == Issue.id)
            .where(Issue.id == issue_id)
            .group_by(Issue.id)
        )
    ).one_or_none()
    if row is None:
        return None
    return ReaderIssueAccess(issue_id=int(row[0]), readable=int(row[1]) > 0)


def _validate_page(*, page: int, per_page: int) -> None:
    if page < 1:
        raise ValueError("Page must be at least 1.")
    if per_page < 1 or per_page > 100:
        raise ValueError("Per-page must be between 1 and 100.")


async def _list_reading_issues(
    session: AsyncSession,
    *,
    filters: tuple[ColumnElement[bool], ...],
    order_by: tuple[ColumnElement[Any], ...],
    page: int,
    per_page: int,
    require_file: bool,
) -> ReadingPage:
    issue_join = IssueReaderState.issue_id == Issue.id
    count_statement = select(func.count(IssueReaderState.id)).join(Issue, issue_join)
    canonical_file = (
        select(
            LibraryFile.issue_id.label("issue_id"),
            func.min(LibraryFile.id).label("file_id"),
        )
        .where(
            LibraryFile.issue_id.is_not(None),
            LibraryFile.file_format.in_(SUPPORTED_READER_FORMATS),
        )
        .group_by(LibraryFile.issue_id)
        .subquery()
    )
    item_statement = (
        select(Issue, Series, LibraryFile, IssueReaderState)
        .join(Issue, issue_join)
        .join(Series, Series.id == Issue.series_id)
    )
    if require_file:
        count_statement = count_statement.join(
            canonical_file,
            canonical_file.c.issue_id == Issue.id,
        )
        item_statement = item_statement.join(
            canonical_file,
            canonical_file.c.issue_id == Issue.id,
        ).join(
            LibraryFile,
            LibraryFile.id == canonical_file.c.file_id,
        )
    else:
        item_statement = item_statement.outerjoin(
            canonical_file,
            canonical_file.c.issue_id == Issue.id,
        ).outerjoin(
            LibraryFile,
            LibraryFile.id == canonical_file.c.file_id,
        )
    total = int((await session.execute(count_statement.where(*filters))).scalar_one())
    rows = (
        await session.execute(
            item_statement.where(*filters)
            .order_by(*order_by)
            .limit(per_page)
            .offset((page - 1) * per_page)
        )
    ).all()
    return ReadingPage(
        items=tuple(_reading_record(*row) for row in rows),
        total=total,
        page=page,
        per_page=per_page,
    )


def _reading_record(
    issue: Issue,
    series: Series,
    library_file: LibraryFile | None,
    state: IssueReaderState,
) -> ReadingIssueRecord:
    readable = (
        issue.status == IssueStatus.OWNED
        and library_file is not None
        and library_file.file_format in SUPPORTED_READER_FORMATS
    )
    return ReadingIssueRecord(
        issue_id=issue.id,
        issue_number=issue.issue_number,
        issue_title=issue.title,
        issue_cover_path=issue.cover_path,
        issue_cover_url=issue.cover_url,
        series_id=series.id,
        series_title=series.title,
        series_year=series.year_start,
        series_cover_path=series.cover_path,
        series_cover_url=series.cover_url,
        readable=readable,
        file_format=library_file.file_format if library_file is not None else None,
        state=_state_projection(state),
    )


def _state_projection(state: IssueReaderState) -> ReadingStateProjection:
    snapshot: ReaderStateSnapshot = snapshot_reader_state(state)
    return ReadingStateProjection(
        last_page_index=snapshot.last_page_index,
        page_count=snapshot.page_count,
        progress_updated_at=snapshot.progress_updated_at,
        last_opened_at=snapshot.last_opened_at,
        completed_at=snapshot.completed_at,
        completion_updated_at=snapshot.completion_updated_at,
        want_to_read=snapshot.want_to_read,
        want_to_read_updated_at=snapshot.want_to_read_updated_at,
        state_version=snapshot.state_version,
    )


def _adjacent_statement(
    *,
    series_id: int,
    boundary: ColumnElement[bool],
    descending: bool,
) -> Select[tuple[int, float, str | None]]:
    issue_order = Issue.issue_number.desc() if descending else Issue.issue_number.asc()
    id_order = Issue.id.desc() if descending else Issue.id.asc()
    return (
        select(Issue.id, Issue.issue_number, Issue.title)
        .join(
            LibraryFile,
            and_(
                LibraryFile.issue_id == Issue.id,
                LibraryFile.file_format.in_(SUPPORTED_READER_FORMATS),
            ),
        )
        .where(
            Issue.series_id == series_id,
            Issue.status == IssueStatus.OWNED,
            boundary,
        )
        .order_by(issue_order, id_order)
        .limit(1)
    )


def _adjacent_reference(
    row: Row[tuple[int, float, str | None]] | None,
) -> AdjacentIssueReference | None:
    if row is None:
        return None
    issue_id, issue_number, title = row
    return AdjacentIssueReference(
        issue_id=int(issue_id),
        issue_number=float(issue_number),
        title=title,
    )
