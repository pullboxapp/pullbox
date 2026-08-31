"""Bounded Mylar3 source paging used by the import scan pipeline."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from pullbox.core.mylar3_reader import Mylar3Reader
from scripts.mylar3_import_fixture import create_mylar3_db

if TYPE_CHECKING:
    from pathlib import Path


def _create_series_rows(count: int) -> list[dict[str, object]]:
    return [
        {
            "ComicID": f"CV-{100_000 + index}",
            "ComicName": f"Series {index:05d}",
            "ComicYear": str(2000 + (index % 25)),
            "ComicPublisher": "Fixture Comics",
            "ComicLocation": f"/unmounted/Series {index:05d}",
            "Total": 2,
        }
        for index in range(count)
    ]


def _create_issue_rows(count: int) -> list[dict[str, object]]:
    return [
        {
            "IssueID": str(((100_000 + series_index) * 100) + issue_number),
            "ComicID": str(100_000 + series_index),
            "ComicName": f"Series {series_index:05d}",
            "IssueName": f"Issue {issue_number}",
            "Issue_Number": str(issue_number),
            "Location": f"Series {series_index:05d} {issue_number:03d}.cbz",
            "IssueDate": "2026-01-01",
        }
        for series_index in range(count)
        for issue_number in (1, 2)
    ]


@pytest.mark.asyncio
async def test_import_series_pages_bound_source_and_issue_cohorts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generated scale never reaches conversion as a whole-table collection."""
    db = tmp_path / "mylar.db"
    series_count = 2_057
    page_size = 17
    create_mylar3_db(
        db,
        series=_create_series_rows(series_count),
        issues=_create_issue_rows(series_count),
        annuals=[],
    )
    reader = Mylar3Reader(db)
    source_page_sizes: list[int] = []
    issue_cohort_sizes: list[int] = []
    original_convert = reader._convert_rows

    def observe_convert(rows, issue_records):
        source_page_sizes.append(len(rows))
        issue_cohort_sizes.append(sum(len(records) for records in issue_records.values()))
        return original_convert(rows, issue_records)

    monkeypatch.setattr(reader, "_convert_rows", observe_convert)

    pages = [page async for page in reader.iter_import_series_pages(page_size=page_size)]
    discovered = [series for page in pages for series in page]

    assert len(pages) == (series_count + page_size - 1) // page_size
    assert max(source_page_sizes) <= page_size
    assert max(issue_cohort_sizes) <= page_size * 2
    assert len(discovered) == series_count
    assert [item.mylar3_cv_id for item in discovered] == [
        100_000 + index for index in range(series_count)
    ]


def _add_story_arcs(db: Path, *, arc_count: int, entries_per_arc: int) -> None:
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            """
            CREATE TABLE storyarcs (
                StoryArcID TEXT,
                StoryArc TEXT,
                ReadingOrder INTEGER,
                IssueID TEXT,
                ComicID TEXT,
                IssueNumber TEXT,
                ComicName TEXT,
                Status TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO storyarcs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    f"arc-{arc_index:05d}",
                    f"Arc {arc_index:05d}",
                    entry_index,
                    f"issue-{arc_index:05d}-{entry_index:03d}",
                    str(100_000 + (arc_index % 257)),
                    str(entry_index),
                    f"Series {arc_index % 257:05d}",
                    "Downloaded",
                )
                for arc_index in range(arc_count)
                for entry_index in range(1, entries_per_arc + 1)
            ],
        )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_import_story_arc_pages_bound_complete_arc_cohorts(tmp_path: Path) -> None:
    """Arc paging is bounded by arc count and never splits one ordered arc."""
    db = tmp_path / "mylar.db"
    create_mylar3_db(db, series=_create_series_rows(257))
    _add_story_arcs(db, arc_count=73, entries_per_arc=3)

    pages = [page async for page in Mylar3Reader(db).iter_import_story_arc_pages(page_size=7)]
    arcs = [arc for page in pages for arc in page]

    assert len(pages) == 11
    assert max(len(page) for page in pages) <= 7
    assert len(arcs) == 73
    assert [arc.story_arc_id for arc in arcs] == [f"arc-{index:05d}" for index in range(73)]
    assert all([entry.ordinal for entry in arc.entries] == [1, 2, 3] for arc in arcs)
    assert all([entry.reading_order for entry in arc.entries] == [1, 2, 3] for arc in arcs)


@pytest.mark.asyncio
async def test_import_series_pages_preserve_standalone_annual_release(tmp_path: Path) -> None:
    """A bounded parent cohort still emits Mylar's separate Annual identity."""
    db = tmp_path / "mylar.db"
    series_dir = tmp_path / "comics" / "X-Men"
    series_dir.mkdir(parents=True)
    regular = series_dir / "X-Men 001 (2021).cbz"
    annual = series_dir / "X-Men Annual 001 (2023).cbz"
    regular.touch()
    annual.touch()
    create_mylar3_db(
        db,
        series=[
            {
                "ComicID": "CV-140553",
                "ComicName": "X-Men",
                "ComicYear": "2021",
                "ComicPublisher": "Marvel",
                "ComicLocation": str(series_dir),
                "Total": 2,
            }
        ],
        issues=[
            {
                "IssueID": "900001",
                "ComicID": "140553",
                "ComicName": "X-Men",
                "IssueName": "Fearless",
                "Issue_Number": "1",
                "Location": regular.name,
                "IssueDate": "2021-09-01",
            }
        ],
        annuals=[
            {
                "IssueID": "950001",
                "ComicID": "140553",
                "ComicName": "X-Men",
                "IssueName": "Contest of Chaos",
                "Issue_Number": "1",
                "Location": annual.name,
                "IssueDate": "2023-08-16",
                "ReleaseComicID": "CV-153726",
                "ReleaseComicName": "X-Men Annual",
            }
        ],
    )

    pages = [page async for page in Mylar3Reader(db).iter_import_series_pages(page_size=1)]
    discovered = [series for page in pages for series in page]

    assert {item.mylar3_cv_id for item in discovered} == {140553, 153726}
    annual_series = next(item for item in discovered if item.mylar3_cv_id == 153726)
    assert [item.file_name for item in annual_series.files] == [annual.name]


@pytest.mark.asyncio
async def test_import_series_pages_merge_annual_into_later_release_series(tmp_path: Path) -> None:
    """A page boundary cannot turn a known release series into a duplicate."""
    db = tmp_path / "mylar.db"
    owner_dir = tmp_path / "comics" / "X-Men"
    release_dir = tmp_path / "comics" / "X-Men Annual"
    owner_dir.mkdir(parents=True)
    release_dir.mkdir(parents=True)
    annual = owner_dir / "X-Men Annual 001 (2023).cbz"
    release_issue = release_dir / "X-Men Annual 002 (2024).cbz"
    annual.touch()
    release_issue.touch()
    create_mylar3_db(
        db,
        series=[
            {
                "ComicID": "CV-140553",
                "ComicName": "X-Men",
                "ComicYear": "2021",
                "ComicPublisher": "Marvel",
                "ComicLocation": str(owner_dir),
                "Total": 1,
            },
            {
                "ComicID": "CV-153726",
                "ComicName": "X-Men Annual",
                "ComicYear": "2023",
                "ComicPublisher": "Marvel",
                "ComicLocation": str(release_dir),
                "Total": 2,
            },
        ],
        issues=[
            {
                "IssueID": "950002",
                "ComicID": "153726",
                "ComicName": "X-Men Annual",
                "IssueName": "The Contest Continues",
                "Issue_Number": "2",
                "Location": release_issue.name,
                "IssueDate": "2024-08-16",
            }
        ],
        annuals=[
            {
                "IssueID": "950001",
                "ComicID": "140553",
                "ComicName": "X-Men",
                "IssueName": "Contest of Chaos",
                "Issue_Number": "1",
                "Location": annual.name,
                "IssueDate": "2023-08-16",
                "ReleaseComicID": "CV-153726",
                "ReleaseComicName": "X-Men Annual",
            }
        ],
    )

    pages = [page async for page in Mylar3Reader(db).iter_import_series_pages(page_size=1)]
    discovered = [series for page in pages for series in page]

    assert [item.mylar3_cv_id for item in discovered] == [140553, 153726]
    owner = discovered[0]
    release = discovered[1]
    assert owner.files == []
    assert {item.file_name for item in release.files} == {annual.name, release_issue.name}
    assert all(item.comicvine_series_id == 153726 for item in release.files)


@pytest.mark.asyncio
async def test_import_series_pages_deduplicate_cv_ids_across_pages(tmp_path: Path) -> None:
    db = tmp_path / "mylar.db"
    rows = _create_series_rows(5)
    rows.append(
        {
            **rows[0],
            "ComicName": "Duplicate that must not cross a page boundary",
        }
    )
    create_mylar3_db(db, series=rows)

    discovered = [
        series
        async for page in Mylar3Reader(db).iter_import_series_pages(page_size=2)
        for series in page
    ]

    assert len(discovered) == 5
    assert discovered[0].raw_series_name == "Series 00000"
