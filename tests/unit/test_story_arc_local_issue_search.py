"""Local-only, bounded Story Arc issue picker contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.series import Series
from pullbox.ui.story_arc_local_issue_search import (
    LOCAL_ISSUE_RESULT_LIMIT,
    search_story_arc_local_issues,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_local_issue_search_is_literal_bounded_and_provider_free(
    db_session: AsyncSession,
) -> None:
    literal_series = Series(title="Literal % Annual", sort_title="literal annual")
    unrelated_series = Series(title="Everything Else", sort_title="everything else")
    db_session.add_all([literal_series, unrelated_series])
    await db_session.flush()
    db_session.add_all(
        [
            Issue(
                series_id=literal_series.id,
                issue_number=float(number),
                title=f"Annual part {number}",
                status=IssueStatus.WANTED,
            )
            for number in range(1, LOCAL_ISSUE_RESULT_LIMIT + 3)
        ]
        + [
            Issue(
                series_id=unrelated_series.id,
                issue_number=1,
                title="A percent-free title",
                status=IssueStatus.WANTED,
            )
        ]
    )
    await db_session.flush()

    result = await search_story_arc_local_issues(
        db_session,
        query="%",
        source_series_name="Literal % Annual",
        source_issue_number_text="2",
    )

    assert len(result.items) == LOCAL_ISSUE_RESULT_LIMIT
    assert result.has_more is True
    assert {item.series_title for item in result.items} == {"Literal % Annual"}
    assert result.items[0].issue_number_text == "2"
    assert all(not hasattr(item, "file_path") for item in result.items)


@pytest.mark.asyncio
async def test_blank_local_issue_search_returns_without_candidates(
    db_session: AsyncSession,
) -> None:
    result = await search_story_arc_local_issues(
        db_session,
        query="   ",
        source_series_name=None,
        source_issue_number_text=None,
    )

    assert result.query == ""
    assert result.items == ()
    assert result.has_more is False


@pytest.mark.asyncio
async def test_exact_number_search_uses_legacy_numeric_fallback_without_suffix_collision(
    db_session: AsyncSession,
) -> None:
    series = Series(title="Suffix Search", sort_title="suffix search")
    db_session.add(series)
    await db_session.flush()
    legacy = Issue(
        series_id=series.id,
        issue_number=2,
        issue_number_text=None,
        title="Legacy numeric issue",
        status=IssueStatus.WANTED,
    )
    suffix = Issue(
        series_id=series.id,
        issue_number=2,
        issue_number_text="2AU",
        title="Suffix issue",
        status=IssueStatus.WANTED,
    )
    db_session.add_all([legacy, suffix])
    await db_session.flush()

    numeric_result = await search_story_arc_local_issues(
        db_session,
        query="2",
        source_series_name=None,
        source_issue_number_text="2",
    )
    suffix_result = await search_story_arc_local_issues(
        db_session,
        query="2AU",
        source_series_name=None,
        source_issue_number_text="2AU",
    )

    assert [item.issue_id for item in numeric_result.items] == [legacy.id]
    assert [item.issue_id for item in suffix_result.items] == [suffix.id]
