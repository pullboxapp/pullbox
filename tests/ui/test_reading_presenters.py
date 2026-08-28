"""Presentation contracts for reusable reading issue cards."""

from __future__ import annotations

from datetime import UTC, datetime

from pullbox.models.library import FileFormat
from pullbox.services.reader_state_service import ReaderStateSnapshot
from pullbox.services.reading_query_service import ReadingIssueRecord, ReadingStateProjection
from pullbox.ui.reading_presenters import present_issue_reading, present_reading_issue


def _record(
    *,
    page_index: int | None = None,
    page_count: int | None = None,
    completed: bool = False,
    explicitly_unread: bool = False,
    want_to_read: bool = False,
    readable: bool = True,
    issue_cover: str | None = "/covers/issue.jpg",
    series_cover: str | None = "/covers/series.jpg",
) -> ReadingIssueRecord:
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    return ReadingIssueRecord(
        issue_id=7,
        issue_number=3.0,
        issue_title="A New Page",
        issue_cover_path=issue_cover,
        issue_cover_url=None,
        series_id=4,
        series_title="Reader Adventures",
        series_year=2026,
        series_cover_path=series_cover,
        series_cover_url=None,
        readable=readable,
        file_format=FileFormat.CBZ if readable else None,
        state=ReadingStateProjection(
            last_page_index=page_index,
            page_count=page_count,
            progress_updated_at=now if page_index is not None else None,
            last_opened_at=now if page_index is not None else None,
            completed_at=now if completed else None,
            completion_updated_at=now if completed or explicitly_unread else None,
            want_to_read=want_to_read,
            want_to_read_updated_at=now if want_to_read else None,
            state_version=3,
        ),
    )


def test_presenter_uses_state_precedence_and_action_labels() -> None:
    in_progress = present_reading_issue(_record(page_index=1, page_count=5))
    queued = present_reading_issue(_record(want_to_read=True))
    completed = present_reading_issue(
        _record(page_index=4, page_count=5, completed=True),
        view="read",
    )
    queued_in_want_to_read = present_reading_issue(
        _record(want_to_read=True),
        view="want-to-read",
    )
    final_unread = present_reading_issue(
        _record(page_index=4, page_count=5, explicitly_unread=True)
    )

    assert in_progress.state_label == "Page 2 of 5 · 40%"
    assert in_progress.primary_label == "Continue"
    assert in_progress.completion_action_label == "Mark read"
    assert in_progress.queue_action_label == "Want to Read"
    assert queued.state_label == "Not started"
    assert queued.primary_label == "Read"
    assert queued.queue_action_label is None
    assert queued.queue_action_value is None
    assert queued_in_want_to_read.queue_action_label == "Remove"
    assert queued_in_want_to_read.queue_action_value is False
    assert completed.state_label == "Read"
    assert completed.primary_label == "Reread"
    assert completed.view == "read"
    assert completed.completion_action_label == "Mark unread"
    assert final_unread.state_label == "Page 5 of 5 · Unread"
    assert final_unread.primary_label == "Read"


def test_presenter_uses_cover_fallbacks_and_unavailable_actions() -> None:
    series_fallback = present_reading_issue(_record(issue_cover=None))
    placeholder = present_reading_issue(
        _record(issue_cover=None, series_cover=None, readable=False, want_to_read=True),
        view="want-to-read",
    )

    assert series_fallback.cover_url == "/covers/series.jpg"
    assert placeholder.cover_url is None
    assert placeholder.state_label == "File unavailable"
    assert placeholder.primary_label == "Open issue"
    assert placeholder.primary_url == "/issues/7"
    assert placeholder.queue_action_label == "Remove"
    assert placeholder.completion_action_label is None


def _snapshot(
    *,
    page_index: int | None = None,
    page_count: int | None = None,
    completed: bool = False,
    explicitly_unread: bool = False,
    want_to_read: bool = False,
) -> ReaderStateSnapshot:
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    return ReaderStateSnapshot(
        user_id=11,
        issue_id=7,
        last_page_index=page_index,
        content_revision="revision" if page_index is not None else None,
        page_count=page_count,
        progress_updated_at=now if page_index is not None else None,
        last_opened_at=now if page_index is not None else None,
        completed_at=now if completed else None,
        completion_updated_at=now if completed or explicitly_unread else None,
        want_to_read=want_to_read,
        want_to_read_updated_at=now if want_to_read else None,
        state_version=4,
        updated_at=now,
    )


def test_issue_presenter_keeps_reading_state_distinct_from_acquisition() -> None:
    absent = present_issue_reading(None, readable=True)
    progress = present_issue_reading(_snapshot(page_index=1, page_count=5), readable=True)
    completed = present_issue_reading(
        _snapshot(page_index=4, page_count=5, completed=True, want_to_read=True),
        readable=True,
    )

    assert absent.state_label is None
    assert absent.primary_label == "Read"
    assert absent.queue_action_label == "Want to Read"
    assert absent.completion_action_label == "Mark read"
    assert progress.state_label == "Page 2 of 5"
    assert progress.primary_label == "Continue"
    assert completed.state_label == "Read"
    assert completed.primary_label == "Read again"
    assert completed.queue_action_label == "In Want to Read"
    assert completed.completion_action_label == "Mark unread"


def test_issue_presenter_omits_completion_action_for_unreadable_issue() -> None:
    view = present_issue_reading(
        _snapshot(want_to_read=True, explicitly_unread=True),
        readable=False,
    )

    assert view.state_label == "Unread"
    assert view.primary_label is None
    assert view.completion_action_label is None
    assert view.queue_action_label == "In Want to Read"
