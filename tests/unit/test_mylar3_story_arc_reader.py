"""Focused source-reader coverage for Mylar story arcs and read lists."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from pullbox.core.mylar3_reader import Mylar3CollectionSnapshot, Mylar3Reader
from scripts.mylar3_import_fixture import create_mylar3_db

if TYPE_CHECKING:
    from pathlib import Path


_STORY_ARC_COLUMNS = (
    "StoryArcID",
    "ComicName",
    "IssueNumber",
    "SeriesYear",
    "IssueYEAR",
    "StoryArc",
    "TotalIssues",
    "Status",
    "inCacheDir",
    "Location",
    "IssueArcID",
    "ReadingOrder",
    "IssueID",
    "ComicID",
    "ReleaseDate",
    "IssueDate",
    "Publisher",
    "IssuePublisher",
    "IssueName",
    "CV_ArcID",
    "Int_IssueNumber",
    "DynamicComicName",
    "Volume",
    "Manual",
    "DateAdded",
    "DigitalDate",
    "Type",
    "Aliases",
    "ArcImage",
)


def _create_source_db(
    db_path: Path,
    *,
    story_arc_rows: list[dict[str, object]] | None = None,
    readlist_count: int | None = None,
) -> None:
    create_mylar3_db(
        db_path,
        series=[
            {
                "ComicID": "CV-42721",
                "ComicName": "Batman",
                "ComicYear": "2011",
            },
            {
                "ComicID": "CV-67824",
                "ComicName": "Saga",
                "ComicYear": "2012",
            },
        ],
    )
    connection = sqlite3.connect(db_path)
    try:
        if story_arc_rows is not None:
            connection.execute(
                """
                CREATE TABLE storyarcs (
                    StoryArcID TEXT, ComicName TEXT, IssueNumber TEXT,
                    SeriesYear TEXT, IssueYEAR TEXT, StoryArc TEXT,
                    TotalIssues TEXT, Status TEXT, inCacheDir TEXT,
                    Location TEXT, IssueArcID TEXT, ReadingOrder INT,
                    IssueID TEXT, ComicID TEXT, ReleaseDate TEXT,
                    IssueDate TEXT, Publisher TEXT, IssuePublisher TEXT,
                    IssueName TEXT, CV_ArcID TEXT, Int_IssueNumber INT,
                    DynamicComicName TEXT, Volume TEXT, Manual TEXT,
                    DateAdded TEXT, DigitalDate TEXT, Type TEXT,
                    Aliases TEXT, ArcImage TEXT
                )
                """
            )
            placeholders = ", ".join("?" for _column in _STORY_ARC_COLUMNS)
            connection.executemany(
                f"INSERT INTO storyarcs VALUES ({placeholders})",
                [tuple(row.get(column) for column in _STORY_ARC_COLUMNS) for row in story_arc_rows],
            )
        if readlist_count is not None:
            connection.execute(
                """
                CREATE TABLE readlist (
                    IssueID TEXT, ComicName TEXT, Issue_Number TEXT,
                    Status TEXT, DateAdded TEXT, Location TEXT,
                    inCacheDir TEXT, SeriesYear TEXT, ComicID TEXT,
                    StatusChange TEXT
                )
                """
            )
            connection.executemany(
                "INSERT INTO readlist (IssueID) VALUES (?)",
                [(f"read-{index}",) for index in range(readlist_count)],
            )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_snapshot_preserves_ordered_multi_series_arc_and_unresolved_entry(
    tmp_path: Path,
) -> None:
    db = tmp_path / "mylar.db"
    existing_arc_file = tmp_path / "Story Arcs" / "Knightfall" / "007-Batman.cbr"
    existing_arc_file.parent.mkdir(parents=True)
    existing_arc_file.write_bytes(b"existing referenced arc placement")
    _create_source_db(
        db,
        story_arc_rows=[
            {
                "StoryArcID": "arc-local-1",
                "CV_ArcID": "4045-12",
                "StoryArc": "Knightfall",
                "IssueArcID": "arc-entry-batman",
                "ReadingOrder": 7,
                "IssueID": "340001",
                "ComicID": "42721",
                "IssueNumber": "001.AU",
                "ComicName": "Batman",
                "SeriesYear": "2011",
                "IssueYEAR": "2012",
                "Status": "Downloaded",
                "Location": str(existing_arc_file),
                "ReleaseDate": "2012-01-04",
                "IssueDate": "2012-01-01",
                "Publisher": "DC Comics",
                "IssuePublisher": "DC Comics",
                "IssueName": "Broken Bat",
                "Manual": "added",
                "DateAdded": "2026-08-29",
                "DigitalDate": "2012-01-04",
                "Type": "Comic",
                "Aliases": "Batman; The Dark Knight",
            },
            {
                "StoryArcID": "arc-local-1",
                "CV_ArcID": "4045-12",
                "StoryArc": "Knightfall",
                "IssueArcID": "arc-entry-saga",
                "ReadingOrder": 2,
                "IssueID": "500001",
                "ComicID": "67824",
                "IssueNumber": "1/2",
                "ComicName": "Saga",
                "SeriesYear": "2012",
                "IssueYEAR": "2012",
                "Status": "Skipped",
                "IssueName": "Chapter One",
                "IssuePublisher": "Image Comics",
            },
            {
                "StoryArcID": "arc-local-1",
                "CV_ArcID": "4045-12",
                "StoryArc": "Knightfall",
                "IssueArcID": "arc-entry-missing",
                "ReadingOrder": 7,
                "IssueID": None,
                "ComicID": None,
                "IssueNumber": "001.BEY",
                "ComicName": "Unresolved Series",
                "SeriesYear": "2019",
                "Status": "Wanted",
                "IssueName": "Missing Chapter",
                "Manual": "added",
            },
        ],
        readlist_count=2,
    )
    source_before = {
        "database": db.read_bytes(),
        "arc_file": existing_arc_file.read_bytes(),
    }

    first = await Mylar3Reader(db).read_snapshot()
    second = await Mylar3Reader(db).read_snapshot()

    assert len(first.series) == 2
    assert first.storyarcs_present is True
    assert first.readlist_present is True
    assert first.readlist_count == 2
    assert len(first.story_arcs) == 1
    arc = first.story_arcs[0]
    assert arc.story_arc_id == "arc-local-1"
    assert arc.cv_arc_id == "4045-12"
    assert arc.name == "Knightfall"
    assert [entry.ordinal for entry in arc.entries] == [1, 2, 3]
    assert [entry.reading_order for entry in arc.entries] == [2, 7, 7]
    assert [entry.issue_number for entry in arc.entries] == ["1/2", "001.AU", "001.BEY"]
    assert [entry.comic_id for entry in arc.entries] == ["67824", "42721", None]
    assert arc.entries[1].issue_id == "340001"
    assert arc.entries[1].location == str(existing_arc_file)
    assert arc.entries[1].issue_name == "Broken Bat"
    assert arc.entries[1].manual == "added"
    assert arc.entries[1].aliases == "Batman; The Dark Knight"
    assert arc.entries[2].issue_arc_id == "arc-entry-missing"
    assert arc.entries[2].issue_id is None
    assert arc.entries[2].status == "Wanted"
    assert first.story_arcs == second.story_arcs
    assert {
        "database": db.read_bytes(),
        "arc_file": existing_arc_file.read_bytes(),
    } == source_before


@pytest.mark.asyncio
async def test_snapshot_tolerates_absent_storyarc_and_readlist_tables(tmp_path: Path) -> None:
    db = tmp_path / "mylar.db"
    _create_source_db(db)

    snapshot = await Mylar3Reader(db).read_snapshot()

    assert snapshot.story_arcs == ()
    assert snapshot.storyarcs_present is False
    assert snapshot.readlist_present is False
    assert snapshot.readlist_count == 0
    assert snapshot.arc_settings.present is False
    assert snapshot.arc_settings.parse_warnings == ()
    settings = {setting.key: setting for setting in snapshot.arc_settings.values}
    assert {key: setting.value for key, setting in settings.items()} == {
        "STORYARCDIR": False,
        "STORYARC_LOCATION": None,
        "COPY2ARCDIR": False,
        "ARC_FOLDERFORMAT": "$arc ($spanyears)",
        "ARC_FILEOPS": "copy",
        "ARC_FILEOPS_SOFTLINK_RELATIVE": False,
        "UPCOMING_STORYARCS": False,
        "SEARCH_STORYARCS": False,
        "READ2FILENAME": False,
    }
    assert all(setting.raw_value is None for setting in settings.values())
    assert all(setting.used_default is True for setting in settings.values())


@pytest.mark.asyncio
async def test_snapshot_tolerates_older_storyarc_columns(tmp_path: Path) -> None:
    db = tmp_path / "mylar.db"
    _create_source_db(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            """
            CREATE TABLE storyarcs (
                StoryArcID TEXT, StoryArc TEXT, ReadingOrder TEXT, IssueNumber TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO storyarcs VALUES (?, ?, ?, ?)",
            ("legacy-arc", "Legacy Arc", "12", "000.1"),
        )
        connection.commit()
    finally:
        connection.close()

    snapshot = await Mylar3Reader(db).read_snapshot()

    assert snapshot.storyarcs_present is True
    assert len(snapshot.story_arcs) == 1
    arc = snapshot.story_arcs[0]
    assert arc.story_arc_id == "legacy-arc"
    assert arc.name == "Legacy Arc"
    assert len(arc.entries) == 1
    assert arc.entries[0].reading_order == 12
    assert arc.entries[0].reading_order_raw == "12"
    assert arc.entries[0].issue_number == "000.1"
    assert arc.entries[0].issue_id is None
    assert arc.entries[0].comic_id is None
    assert arc.entries[0].location is None


@pytest.mark.asyncio
async def test_readlist_is_counted_but_not_converted_to_story_arcs(tmp_path: Path) -> None:
    db = tmp_path / "mylar.db"
    _create_source_db(db, story_arc_rows=[], readlist_count=3)

    snapshot = await Mylar3Reader(db).read_snapshot()

    assert snapshot.storyarcs_present is True
    assert snapshot.story_arcs == ()
    assert snapshot.readlist_present is True
    assert snapshot.readlist_count == 3


@pytest.mark.asyncio
async def test_read_collection_uses_one_read_only_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "mylar.db"
    _create_source_db(db, story_arc_rows=[], readlist_count=0)
    original_connect = sqlite3.connect
    connections: list[tuple[str, bool]] = []

    def counting_connect(database: str, *, uri: bool = False) -> sqlite3.Connection:
        connections.append((database, uri))
        return original_connect(database, uri=uri)

    monkeypatch.setattr("pullbox.core.mylar3_reader.sqlite3.connect", counting_connect)

    snapshot = await Mylar3Reader(db).read_collection()

    assert isinstance(snapshot, Mylar3CollectionSnapshot)
    assert connections == [(f"{db.resolve().as_uri()}?mode=ro", True)]


@pytest.mark.asyncio
async def test_snapshot_discovers_current_upstream_arc_settings_without_secrets(
    tmp_path: Path,
) -> None:
    db = tmp_path / "mylar.db"
    _create_source_db(db)
    config = tmp_path / "config.ini"
    config.write_text(
        """
[General]
READ2FILENAME = 1
COMICVINE_API = do-not-leak-api-key

[StoryArc]
STORYARCDIR = true
STORYARC_LOCATION = /comics/StoryArcs
COPY2ARCDIR = yes
ARC_FOLDERFORMAT = $arc ($spanyears)
ARC_FILEOPS = softlink
ARC_FILEOPS_SOFTLINK_RELATIVE = on
UPCOMING_STORYARCS = true
SEARCH_STORYARCS = 1

[NZBGet]
Password = do-not-leak-password
""".strip()
    )

    snapshot = await Mylar3Reader(db).read_snapshot()

    arc_settings = snapshot.arc_settings
    settings = {setting.key: setting for setting in arc_settings.values}
    assert arc_settings.present is True
    assert arc_settings.parse_warnings == ()
    assert {key: setting.value for key, setting in settings.items()} == {
        "STORYARCDIR": True,
        "STORYARC_LOCATION": "/comics/StoryArcs",
        "COPY2ARCDIR": True,
        "ARC_FOLDERFORMAT": "$arc ($spanyears)",
        "ARC_FILEOPS": "softlink",
        "ARC_FILEOPS_SOFTLINK_RELATIVE": True,
        "UPCOMING_STORYARCS": True,
        "SEARCH_STORYARCS": True,
        "READ2FILENAME": True,
    }
    assert {setting.section for setting in arc_settings.values} == {"General", "StoryArc"}
    assert all(setting.used_default is False for setting in arc_settings.values)
    serialized_snapshot = repr(arc_settings)
    assert "do-not-leak-api-key" not in serialized_snapshot
    assert "do-not-leak-password" not in serialized_snapshot
    assert "COMICVINE_API" not in serialized_snapshot
    assert "Password" not in serialized_snapshot


@pytest.mark.asyncio
async def test_unknown_arc_file_operation_is_retained_for_review(tmp_path: Path) -> None:
    db = tmp_path / "mylar.db"
    _create_source_db(db)
    config = tmp_path / "custom-config.ini"
    config.write_text(
        "[StoryArc]\nARC_FILEOPS = teleport\nARC_FOLDERFORMAT = %(arc_name)s/%(year)s\n"
    )

    snapshot = await Mylar3Reader(db, config_path=config).read_snapshot()

    settings = {setting.key: setting for setting in snapshot.arc_settings.values}
    arc_fileops = settings["ARC_FILEOPS"]
    assert snapshot.arc_settings.present is True
    assert snapshot.arc_settings.parse_warnings == ("unknown_value:ARC_FILEOPS",)
    assert arc_fileops.value == "teleport"
    assert arc_fileops.raw_value == "teleport"
    assert arc_fileops.used_default is False
    assert settings["ARC_FOLDERFORMAT"].value == "%(arc_name)s/%(year)s"
    assert settings["ARC_FOLDERFORMAT"].raw_value == "%(arc_name)s/%(year)s"


@pytest.mark.asyncio
async def test_oversize_arc_config_is_rejected_without_partial_values(tmp_path: Path) -> None:
    db = tmp_path / "mylar.db"
    _create_source_db(db)
    config = tmp_path / "oversize-config.ini"
    config.write_bytes(b"[StoryArc]\nARC_FILEOPS=hardlink\n" + b"x" * Mylar3Reader.MAX_CONFIG_BYTES)

    snapshot = await Mylar3Reader(db, config_path=config).read_snapshot()

    assert snapshot.arc_settings.present is True
    assert snapshot.arc_settings.parse_warnings == ("config_too_large",)
    settings = {setting.key: setting for setting in snapshot.arc_settings.values}
    assert settings["ARC_FILEOPS"].value == "copy"
    assert settings["ARC_FILEOPS"].raw_value is None
    assert all(setting.used_default is True for setting in settings.values())


@pytest.mark.asyncio
async def test_symlinked_arc_config_is_rejected_without_reading_target(tmp_path: Path) -> None:
    db = tmp_path / "mylar.db"
    _create_source_db(db)
    target = tmp_path / "outside-config.ini"
    target.write_text("[StoryArc]\nARC_FILEOPS=hardlink\n[General]\nAPI_KEY=symlink-secret\n")
    (tmp_path / "config.ini").symlink_to(target)

    snapshot = await Mylar3Reader(db).read_snapshot()

    assert snapshot.arc_settings.present is True
    assert snapshot.arc_settings.parse_warnings == ("config_symlink_rejected",)
    assert "symlink-secret" not in repr(snapshot.arc_settings)
    settings = {setting.key: setting for setting in snapshot.arc_settings.values}
    assert settings["ARC_FILEOPS"].value == "copy"
    assert settings["ARC_FILEOPS"].raw_value is None
