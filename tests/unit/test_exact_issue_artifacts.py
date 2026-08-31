"""Exact issue identity must survive naming, metadata writes, and reader loading."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.issue_numbers import parse_issue_number_text
from pullbox.core.library_comicinfo import build_comicinfo_payload_for_issue
from pullbox.core.library_naming import compute_target_filename
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot
from pullbox.models.series import Series
from pullbox.services.import_file_preparation import (
    build_comicinfo_payload_for_issue as build_import_comicinfo,
)
from pullbox.services.reader_content_service import load_reader_source_record


@pytest.fixture(params=["1", "1AU", "12.25", "1000000", "100000000000000000001"])
async def exact_issue(request: pytest.FixtureRequest, db_session: AsyncSession) -> Issue:
    series = Series(title="Exact Series", sort_title="exact series", year_start=2026)
    db_session.add(series)
    await db_session.flush()
    # Seed the persisted dual representation, including values beyond float precision.
    numeric, exact = parse_issue_number_text(request.param)
    issue_id = await db_session.scalar(
        insert(Issue)
        .values(
            series_id=series.id,
            issue_number=numeric,
            issue_number_text=exact,
            status=IssueStatus.OWNED,
        )
        .returning(Issue.id)
    )
    issue = await db_session.get(Issue, issue_id)
    assert issue is not None
    return issue


@pytest.mark.parametrize("padded", [False, True])
async def test_target_filename_preserves_exact_identity(
    db_session: AsyncSession, exact_issue: Issue, padded: bool
) -> None:
    series = await db_session.get(Series, exact_issue.series_id)
    assert series is not None
    token = "{Issue:03d}" if padded else "{Issue}"
    exact = exact_issue.effective_issue_number_text
    expected = (
        {"1": "001", "1AU": "001AU", "12.25": "012.25"}.get(exact, exact) if padded else exact
    )
    assert (
        compute_target_filename(
            exact_issue, series, Path("source.cbz"), {"comic_file_template": f"{{Series}} #{token}"}
        )
        == f"Exact Series #{expected}.cbz"
    )


@pytest.mark.parametrize("builder", [build_comicinfo_payload_for_issue, build_import_comicinfo])
async def test_comicinfo_preserves_exact_identity(
    db_session: AsyncSession,
    exact_issue: Issue,
    builder: Callable[[AsyncSession, Issue], Awaitable[dict[str, Any]]],
) -> None:
    payload = await builder(db_session, exact_issue)
    assert payload["Number"] == exact_issue.effective_issue_number_text


async def test_reader_record_preserves_exact_identity(
    db_session: AsyncSession, exact_issue: Issue, tmp_path: Path
) -> None:
    root = LibraryRoot(name="Exact Root", path=str(tmp_path))
    db_session.add(root)
    await db_session.flush()
    db_session.add(
        LibraryFile(
            issue_id=exact_issue.id,
            library_root_id=root.id,
            file_path=str(tmp_path / "source.cbz"),
            file_name="source.cbz",
            file_size=10,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
        )
    )
    await db_session.flush()
    record = await load_reader_source_record(db_session, exact_issue.id)
    assert record.issue_number == exact_issue.effective_issue_number_text
    assert record.issue_number_value == exact_issue.issue_number
