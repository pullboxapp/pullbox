"""Tests for the bounded, read-only Story Arc Step 1 preflight."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from pullbox.models.import_job import ImportSourceType
from pullbox.services.import_story_arc_preflight import StoryArcPreflightAnalyzer
from scripts.mylar3_import_fixture import create_mylar3_db

if TYPE_CHECKING:
    from pathlib import Path


def _add_mylar_story_arc_tables(db_path: Path, existing_arc_file: Path) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE storyarcs (
                StoryArcID TEXT, StoryArc TEXT, ReadingOrder INTEGER,
                ComicName TEXT, IssueNumber TEXT, IssueName TEXT,
                IssueID TEXT, ComicID TEXT, IssueArcID TEXT,
                CV_ArcID TEXT, Status TEXT, Location TEXT
            );
            CREATE TABLE readlist (IssueID TEXT);
            """
        )
        connection.executemany(
            """
            INSERT INTO storyarcs (
                StoryArcID, StoryArc, ReadingOrder, ComicName, IssueNumber,
                IssueName, IssueID, ComicID, IssueArcID, CV_ArcID, Status, Location
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "arc-1",
                    "Knightfall",
                    1,
                    "Batman",
                    "497",
                    "Broken Bat",
                    "1001",
                    "42721",
                    "entry-1",
                    "4045-12",
                    "Downloaded",
                    str(existing_arc_file),
                ),
                (
                    "arc-1",
                    "Knightfall",
                    2,
                    "Catwoman",
                    "14",
                    "Aftermath",
                    "1002",
                    "50000",
                    "entry-2",
                    "4045-12",
                    "Downloaded",
                    None,
                ),
                (
                    "arc-1",
                    "Knightfall (legacy label)",
                    2,
                    "Catwoman",
                    "14",
                    "Duplicate saved order",
                    "1002-duplicate",
                    "50000",
                    "entry-2-duplicate",
                    "4045-12",
                    "Downloaded",
                    None,
                ),
                (
                    "arc-2",
                    "Dark Nights: Metal",
                    1,
                    "Dark Nights: Metal",
                    "1",
                    "Dark Days",
                    None,
                    None,
                    "entry-3",
                    "4045-99",
                    "Wanted",
                    None,
                ),
            ],
        )
        connection.executemany(
            "INSERT INTO readlist (IssueID) VALUES (?)",
            [("read-1",), ("read-2",)],
        )
        connection.commit()
    finally:
        connection.close()


async def test_mylar_database_symlink_preserves_selected_folder_settings(tmp_path: Path) -> None:
    database = tmp_path / "actual.db"
    create_mylar3_db(database)
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "mylar.db").symlink_to(database)
    (selected / "config.ini").write_text("[General]\nREAD2FILENAME = true\n")
    result = await StoryArcPreflightAnalyzer().analyze(
        selected, source_type=ImportSourceType.MYLAR3
    )
    assert any(
        setting.key == "READ2FILENAME" and setting.value is True for setting in result.settings
    )


@pytest.mark.asyncio
async def test_mylar_preflight_reports_counts_settings_and_sanitized_examples(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "mylar.db"
    create_mylar3_db(db_path)
    existing_arc_file = tmp_path / "Story Arcs" / "Knightfall" / "001 - Batman 497.cbz"
    existing_arc_file.parent.mkdir(parents=True)
    existing_arc_file.write_bytes(b"existing")
    _add_mylar_story_arc_tables(db_path, existing_arc_file)
    (tmp_path / "config.ini").write_text(
        """
[StoryArc]
STORYARCDIR = true
STORYARC_LOCATION = /private/story-arcs
COPY2ARCDIR = true
ARC_FOLDERFORMAT = $arc ($spanyears)
ARC_FILEOPS = copy
[General]
READ2FILENAME = true
""".strip()
    )
    source_before = db_path.read_bytes()

    result = await StoryArcPreflightAnalyzer().analyze(
        db_path,
        source_type=ImportSourceType.MYLAR3,
    )

    assert result.evidence_detected is True
    assert result.arcs_detected == 2
    assert result.entries_detected == 4
    assert result.resolution.missing == 1
    assert result.resolution.resolved == 0
    assert result.resolution.pending == 3
    assert result.resolution.duplicates == 1
    assert result.existing_arc_files_detected is True
    assert result.existing_arc_folders_detected is True
    assert result.provider_calls_required is False
    assert result.provider_call_summary == "No provider calls are needed for trusted Mylar data."
    assert result.readlist_present is True
    assert result.readlist_count == 2
    assert result.readlist_import_state == "deferred_v1.5.0"
    assert result.proposed_policy.mode == "copy"
    assert result.proposed_policy.destination_root_configured is True
    assert result.proposed_policy.folder_template == "{StoryArc} ({SpanYears})"
    assert result.proposed_policy.reading_order_prefix is True
    assert result.examples[0].story_arc == "Dark Nights: Metal"
    assert all(example.relative_path is None for example in result.examples)
    assert "/private/story-arcs" not in repr(result)
    assert str(tmp_path) not in repr(result)
    assert db_path.read_bytes() == source_before


@pytest.mark.asyncio
async def test_folder_preflight_detects_bounded_reading_order_pattern_without_archive_probe(
    tmp_path: Path,
) -> None:
    arc_folder = tmp_path / "Knightfall"
    arc_folder.mkdir()
    (arc_folder / "001 - Batman 497.cbz").write_bytes(b"fixture")
    (arc_folder / "002 - Catwoman 014.cbz").write_bytes(b"fixture")

    result = await StoryArcPreflightAnalyzer().analyze(
        tmp_path,
        source_type=ImportSourceType.FILESYSTEM,
    )

    assert result.evidence_detected is True
    assert result.arcs_detected == 1
    assert result.entries_detected == 2
    assert result.resolution.pending == 2
    assert result.pattern_summary == "Reading-order prefixes across multiple series"
    assert result.provider_calls_required is True
    assert result.archive_probes == 0
    assert result.partial is True
    assert "full_scan_may_find_additional_comicinfo_evidence" in result.warnings
    assert [example.relative_path for example in result.examples] == [
        "Knightfall/001 - Batman 497.cbz",
        "Knightfall/002 - Catwoman 014.cbz",
    ]


@pytest.mark.asyncio
async def test_folder_preflight_keeps_normal_series_folder_out_of_story_arc_controls(
    tmp_path: Path,
) -> None:
    series_folder = tmp_path / "Batman (2011)"
    series_folder.mkdir()
    (series_folder / "Batman 001.cbz").write_bytes(b"fixture")

    result = await StoryArcPreflightAnalyzer().analyze(
        tmp_path,
        source_type=ImportSourceType.FILESYSTEM,
    )

    assert result.evidence_detected is False
    assert result.arcs_detected == 0
    assert result.entries_detected == 0
