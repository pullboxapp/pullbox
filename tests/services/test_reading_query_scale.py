"""Large-catalog evidence for bounded private reading queries."""

from __future__ import annotations

import statistics
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event, insert, select

from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.reader import IssueReaderState
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.models.user import User
from pullbox.services.reading_query_service import list_continue_reading

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


def test_reader_query_join_has_library_file_issue_index() -> None:
    assert "ix_library_files_issue" in {index.name for index in LibraryFile.__table__.indexes}


@pytest.mark.asyncio
async def test_continue_query_stays_bounded_for_ten_thousand_issue_catalog(
    async_engine: AsyncEngine,
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    user = User(username="scale-reader", password_hash="test-hash")
    other_user = User(username="scale-other-reader", password_hash="test-hash")
    root = LibraryRoot(name="Scale Library", path="/scale-library", enabled=True)
    db_session.add_all((user, other_user, root))
    await db_session.flush()

    await db_session.execute(
        insert(Series),
        [
            {
                "title": f"Scale Series {series_index:03d}",
                "sort_title": f"scale series {series_index:03d}",
                "year_start": 2026,
                "status": SeriesStatus.CONTINUING,
                "series_type": SeriesType.STANDARD,
                "monitored": True,
                "issue_count": 20,
                "library_root_id": root.id,
            }
            for series_index in range(500)
        ],
    )
    series_ids = tuple((await db_session.execute(select(Series.id).order_by(Series.id))).scalars())
    await db_session.execute(
        insert(Issue),
        [
            {
                "series_id": series_id,
                "issue_number": float(issue_number),
                "title": f"Scale Issue {issue_number}",
                "status": IssueStatus.OWNED,
            }
            for series_id in series_ids
            for issue_number in range(1, 21)
        ],
    )
    issue_ids = tuple((await db_session.execute(select(Issue.id).order_by(Issue.id))).scalars())
    assert len(issue_ids) == 10_000

    supported_formats = (FileFormat.CBZ, FileFormat.CBR, FileFormat.PDF)
    await db_session.execute(
        insert(LibraryFile),
        [
            {
                "file_path": f"/scale-library/{issue_id}.{file_format.value}",
                "file_name": f"{issue_id}.{file_format.value}",
                "file_size": 1024,
                "file_format": file_format,
                "file_modified_at": now,
                "match_confidence": MatchConfidence.HIGH,
                "issue_id": issue_id,
                "library_root_id": root.id,
            }
            for offset, issue_id in enumerate(issue_ids[:7500])
            for file_format in (
                supported_formats[offset % len(supported_formats)]
                if offset % 5
                else FileFormat.EPUB,
            )
        ],
    )

    state_rows = [
        {
            "user_id": user.id,
            "issue_id": issue_id,
            "last_page_index": issue_id % 7,
            "content_revision": f"revision-{issue_id}",
            "page_count": 10,
            "progress_updated_at": now - timedelta(seconds=issue_id),
            "last_opened_at": now - timedelta(seconds=issue_id),
            "completed_at": now if issue_id % 11 == 0 else None,
            "completion_updated_at": now if issue_id % 11 == 0 else None,
            "want_to_read": issue_id % 13 == 0,
            "want_to_read_updated_at": now if issue_id % 13 == 0 else None,
            "state_version": 1,
        }
        for issue_id in issue_ids[:5000]
    ]
    await db_session.execute(insert(IssueReaderState), state_rows)
    await db_session.execute(
        insert(IssueReaderState),
        [
            {
                **row,
                "user_id": other_user.id,
                "content_revision": f"other-{row['issue_id']}",
            }
            for row in state_rows
        ],
    )
    await db_session.commit()

    await list_continue_reading(db_session, user_id=user.id, page=1, per_page=8)
    statements: list[str] = []

    def record_statement(*args: object) -> None:
        statements.append(str(args[2]))

    event.listen(async_engine.sync_engine, "before_cursor_execute", record_statement)
    durations: list[float] = []
    try:
        for _sample in range(3):
            started = time.perf_counter()
            page = await list_continue_reading(
                db_session,
                user_id=user.id,
                page=1,
                per_page=8,
            )
            durations.append(time.perf_counter() - started)
        tracemalloc.start()
        page = await list_continue_reading(
            db_session,
            user_id=user.id,
            page=1,
            per_page=8,
        )
        _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        event.remove(async_engine.sync_engine, "before_cursor_execute", record_statement)

    assert len(statements) == 8
    assert len(page.items) == 8
    assert page.total > len(page.items)
    assert all(item.state.is_continue_candidate for item in page.items)
    assert all(item.readable for item in page.items)
    print(
        "reader_query_scale "
        f"issues=10000 states_per_user=5000 rows={len(page.items)} "
        "statements_per_sample=2 "
        f"warmed_median_ms={statistics.median(durations) * 1000:.3f} "
        f"tracemalloc_peak_bytes={peak_bytes}"
    )
