"""Deterministic Mylar database and multi-root acceptance fixture generator."""

from __future__ import annotations

import json
import shutil
import sqlite3
from typing import TYPE_CHECKING

from .shared import (
    CbrSeedEvidence,
    consume_cbr_seed_set,
    create_deterministic_cbz,
    deterministic_jpeg,
    prepare_fixture_root,
    snapshot_tree,
    write_bytes,
    write_manifest,
    write_text,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

_SCHEMA_VERSION = 1
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


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _create_base_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE comics (
            ComicID TEXT,
            ComicName TEXT,
            ComicYear TEXT,
            ComicPublisher TEXT,
            ComicLocation TEXT,
            Status TEXT,
            Total INTEGER,
            ComicImage TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE issues (
            IssueID TEXT,
            ComicName TEXT,
            IssueName TEXT,
            Issue_Number TEXT,
            ComicID TEXT,
            Location TEXT,
            IssueDate TEXT,
            Int_IssueNumber INTEGER
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE annuals (
            IssueID TEXT,
            Issue_Number TEXT,
            IssueName TEXT,
            IssueDate TEXT,
            ComicID TEXT,
            Location TEXT,
            Int_IssueNumber INTEGER,
            ComicName TEXT,
            ReleaseComicID TEXT,
            ReleaseComicName TEXT
        )
        """
    )


def _create_full_optional_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE storyarcs (
            StoryArcID TEXT, ComicName TEXT, IssueNumber TEXT,
            SeriesYear TEXT, IssueYEAR TEXT, StoryArc TEXT,
            TotalIssues TEXT, Status TEXT, inCacheDir TEXT,
            Location TEXT, IssueArcID TEXT, ReadingOrder INTEGER,
            IssueID TEXT, ComicID TEXT, ReleaseDate TEXT,
            IssueDate TEXT, Publisher TEXT, IssuePublisher TEXT,
            IssueName TEXT, CV_ArcID TEXT, Int_IssueNumber INTEGER,
            DynamicComicName TEXT, Volume TEXT, Manual TEXT,
            DateAdded TEXT, DigitalDate TEXT, Type TEXT,
            Aliases TEXT, ArcImage TEXT
        )
        """
    )
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


def _create_database(
    path: Path,
    *,
    comics: Sequence[dict[str, object]],
    issues: Sequence[dict[str, object]],
    annuals: Sequence[dict[str, object]],
    optional_mode: str,
    story_arcs: Sequence[dict[str, object]] = (),
    readlist: Sequence[dict[str, object]] = (),
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA page_size = 4096")
        connection.execute("PRAGMA journal_mode = DELETE")
        _create_base_tables(connection)
        connection.executemany(
            "INSERT INTO comics VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    row.get("ComicID"),
                    row.get("ComicName"),
                    row.get("ComicYear"),
                    row.get("ComicPublisher"),
                    row.get("ComicLocation"),
                    row.get("Status", "Active"),
                    row.get("Total", 0),
                    row.get("ComicImage"),
                )
                for row in comics
            ],
        )
        connection.executemany(
            "INSERT INTO issues VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    row.get("IssueID"),
                    row.get("ComicName"),
                    row.get("IssueName"),
                    row.get("Issue_Number"),
                    row.get("ComicID"),
                    row.get("Location"),
                    row.get("IssueDate"),
                    row.get("Int_IssueNumber"),
                )
                for row in issues
            ],
        )
        connection.executemany(
            "INSERT INTO annuals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    row.get("IssueID"),
                    row.get("Issue_Number"),
                    row.get("IssueName"),
                    row.get("IssueDate"),
                    row.get("ComicID"),
                    row.get("Location"),
                    row.get("Int_IssueNumber"),
                    row.get("ComicName"),
                    row.get("ReleaseComicID"),
                    row.get("ReleaseComicName"),
                )
                for row in annuals
            ],
        )
        if optional_mode == "full":
            _create_full_optional_tables(connection)
            placeholders = ", ".join("?" for _column in _STORY_ARC_COLUMNS)
            connection.executemany(
                f"INSERT INTO storyarcs VALUES ({placeholders})",
                [tuple(row.get(column) for column in _STORY_ARC_COLUMNS) for row in story_arcs],
            )
            connection.executemany(
                "INSERT INTO readlist VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        row.get("IssueID"),
                        row.get("ComicName"),
                        row.get("Issue_Number"),
                        row.get("Status"),
                        row.get("DateAdded"),
                        row.get("Location"),
                        row.get("inCacheDir"),
                        row.get("SeriesYear"),
                        row.get("ComicID"),
                        row.get("StatusChange"),
                    )
                    for row in readlist
                ],
            )
        elif optional_mode == "legacy":
            connection.execute(
                """
                CREATE TABLE storyarcs (
                    StoryArcID TEXT,
                    StoryArc TEXT,
                    ReadingOrder TEXT,
                    IssueNumber TEXT
                )
                """
            )
            connection.execute(
                "INSERT INTO storyarcs VALUES (?, ?, ?, ?)",
                ("legacy-arc", "Legacy Fixture Arc", "1", "0.5"),
            )
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
            connection.execute(
                "INSERT INTO readlist (IssueID, ComicName, Issue_Number) VALUES (?, ?, ?)",
                ("legacy-read-1", "Number Lab", "0.5"),
            )
        elif optional_mode != "absent":
            raise ValueError(f"Unsupported Mylar optional-table mode: {optional_mode}")
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    path.chmod(0o644)
    return path


def generate_mylar_fixture(
    output_root: Path,
    *,
    seed: int = 1300,
    cbr_seed_dir: Path | None = None,
    cbr_expected_sha256: dict[str, str] | None = None,
) -> Path:
    """Generate Mylar databases, mounted roots, sidecars, and case evidence."""
    root = prepare_fixture_root(output_root)
    library_a = root / "roots" / "library-a"
    library_b = root / "roots" / "library-b"
    story_arc_root = root / "roots" / "story-arcs"
    for directory in (library_a, library_b, story_arc_root):
        directory.mkdir(parents=True)

    def add_archive(
        actual_root: Path,
        relative_path: str,
        *,
        case_id: str,
        series: str,
        number: str,
        title: str,
        year: int,
        publisher: str,
        comicvine_series_id: int,
        comicvine_issue_id: int,
    ) -> Path:
        path = actual_root / relative_path
        return create_deterministic_cbz(
            path,
            seed=seed,
            case_id=case_id,
            series=series,
            number=number,
            title=title,
            year=year,
            publisher=publisher,
            comicvine_series_id=comicvine_series_id,
            comicvine_issue_id=comicvine_issue_id,
        )

    identity_dir = "DC Comics/Identity Series (2020)"
    identity_one = add_archive(
        library_a,
        f"{identity_dir}/Identity Series 001 (2020).cbz",
        case_id="mylar_identity_1",
        series="Identity Series",
        number="1",
        title="First Identity",
        year=2020,
        publisher="DC Comics",
        comicvine_series_id=810001,
        comicvine_issue_id=810001001,
    )
    identity_two = add_archive(
        library_a,
        f"{identity_dir}/Identity Series 002 (2020).cbz",
        case_id="mylar_identity_2",
        series="Identity Series",
        number="2",
        title="Second Identity",
        year=2020,
        publisher="DC Comics",
        comicvine_series_id=810001,
        comicvine_issue_id=810001002,
    )

    mapped_dir = "Image/Mapped Series (2021)"
    mapped_one = add_archive(
        library_b,
        f"{mapped_dir}/Mapped Series 001 (2021).cbz",
        case_id="mylar_mapped_1",
        series="Mapped Series",
        number="1",
        title="Mapped",
        year=2021,
        publisher="Image Comics",
        comicvine_series_id=810002,
        comicvine_issue_id=810002001,
    )
    add_archive(
        library_b,
        "No Location Series (2017)/No Location Series 000 (2017).cbz",
        case_id="mylar_no_location",
        series="No Location Series",
        number="0",
        title="Found by Absolute Issue Path",
        year=2017,
        publisher="Fixture House",
        comicvine_series_id=810003,
        comicvine_issue_id=810003000,
    )

    split_dir_a = "Fixture House/Split Series (2022)"
    split_one = add_archive(
        library_a,
        f"{split_dir_a}/Split Series 001 (2022).cbz",
        case_id="mylar_split_1",
        series="Split Series",
        number="1",
        title="Root A",
        year=2022,
        publisher="Fixture House",
        comicvine_series_id=810004,
        comicvine_issue_id=810004001,
    )
    add_archive(
        library_b,
        "Split Overflow/Split Series 002 (2022).cbz",
        case_id="mylar_split_2",
        series="Split Series",
        number="2",
        title="Root B",
        year=2022,
        publisher="Fixture House",
        comicvine_series_id=810004,
        comicvine_issue_id=810004002,
    )

    number_dir = "Fixture House/Number Lab (2026)"
    number_values = ("0", ".5", "0.5", "1/2", "1A", "10000", "1000000")
    number_files: dict[str, Path] = {}
    filename_number = {
        "0": "0",
        ".5": "dot5",
        "0.5": "0.5",
        "1/2": "one-half",
        "1A": "1A",
        "10000": "10000",
        "1000000": "1000000",
    }
    for index, number in enumerate(number_values, start=1):
        number_files[number] = add_archive(
            library_a,
            f"{number_dir}/Number Lab Issue {filename_number[number]}.cbz",
            case_id=f"mylar_number_{index}",
            series="Number Lab",
            number=number,
            title=f"Number {number}",
            year=2026,
            publisher="Fixture House",
            comicvine_series_id=810005,
            comicvine_issue_id=810005000 + index,
        )

    annual_dir = "Marvel/Annual Owner (2023)"
    annual_regular = add_archive(
        library_a,
        f"{annual_dir}/Annual Owner 001 (2023).cbz",
        case_id="mylar_annual_regular",
        series="Annual Owner",
        number="1",
        title="Regular",
        year=2023,
        publisher="Marvel",
        comicvine_series_id=810009,
        comicvine_issue_id=810009001,
    )
    annual_file = add_archive(
        library_a,
        f"{annual_dir}/Annual Owner Annual 001 (2024).cbz",
        case_id="mylar_annual",
        series="Annual Owner Annual",
        number="1",
        title="Annual Event",
        year=2024,
        publisher="Marvel",
        comicvine_series_id=910009,
        comicvine_issue_id=910009001,
    )

    cbr_manifest: dict[str, object] = {
        "status": "not_provided",
        "required_filenames": ["iu9-rar3.cbr", "iu9-rar5.cbr"],
    }
    cbr_evidence: tuple[CbrSeedEvidence, ...] = ()
    if cbr_seed_dir is not None:
        cbr_evidence = consume_cbr_seed_set(
            cbr_seed_dir,
            library_b / "Fixture House" / "Mapped CBR Series (2025)",
            expected_sha256=cbr_expected_sha256,
            destination_filenames={
                "rar3": "Mapped CBR Series 001 (2025).cbr",
                "rar5": "Mapped CBR Series 002 (2025).cbr",
            },
        )
        cbr_manifest = {
            "status": "provided",
            "seeds": [item.to_manifest(root) for item in cbr_evidence],
        }

    series_sidecar: dict[str, object] = {
        "comicid": 810001,
        "name": "Identity Series",
        "year": 2020,
        "comicvine": {
            "id": 810001,
            "url": "https://comicvine.gamespot.com/volume/4050-810001/",
        },
    }
    write_text(
        library_a / identity_dir / "series.json",
        json.dumps(series_sidecar, indent=2, sort_keys=True) + "\n",
    )
    write_text(
        library_a / identity_dir / "cvinfo",
        "comicid: 810001\nurl: https://comicvine.gamespot.com/volume/4050-810001/\n",
    )
    write_bytes(library_a / identity_dir / "cover.jpg", deterministic_jpeg())

    comics: list[dict[str, object]] = [
        {
            "ComicID": "CV-810001",
            "ComicName": "Identity Series",
            "ComicYear": "2020",
            "ComicPublisher": "DC Comics",
            "ComicLocation": "/iu9/mylar-a/DC Comics/Identity Series (2020)",
            "Total": 2,
        },
        {
            "ComicID": "810002",
            "ComicName": "Mapped Series",
            "ComicYear": "2021",
            "ComicPublisher": "Image Comics",
            "ComicLocation": "/legacy/comics-b/Image/Mapped Series (2021)",
            "Total": 1,
        },
        {
            "ComicID": "CV-810003",
            "ComicName": "No Location Series",
            "ComicYear": "2017",
            "ComicPublisher": "Fixture House",
            "ComicLocation": None,
            "Total": 1,
        },
        {
            "ComicID": "CV-810004",
            "ComicName": "Split Series",
            "ComicYear": "2022",
            "ComicPublisher": "Fixture House",
            "ComicLocation": "/iu9/mylar-a/Fixture House/Split Series (2022)",
            "Total": 2,
        },
        {
            "ComicID": "CV-810005",
            "ComicName": "Number Lab",
            "ComicYear": "2026",
            "ComicPublisher": "Fixture House",
            "ComicLocation": "/iu9/mylar-a/Fixture House/Number Lab (2026)",
            "Total": len(number_values),
        },
        {
            "ComicID": "CV-810006",
            "ComicName": "Missing Root",
            "ComicYear": "2018",
            "ComicPublisher": "Fixture House",
            "ComicLocation": "/offline/library/Missing Root (2018)",
            "Total": 1,
        },
        {
            "ComicID": "CV-810001",
            "ComicName": "Duplicate Identity",
            "ComicYear": "2020",
            "ComicPublisher": "Fixture House",
            "ComicLocation": "/iu9/mylar-b/Duplicate Identity (2020)",
            "Total": 0,
        },
        {
            "ComicID": "CV-not-a-number",
            "ComicName": "Malformed Identity",
            "ComicYear": "unknown",
            "ComicPublisher": "Fixture House",
            "ComicLocation": None,
            "Total": 0,
        },
        {
            "ComicID": "CV-810009",
            "ComicName": "Annual Owner",
            "ComicYear": "2023",
            "ComicPublisher": "Marvel",
            "ComicLocation": "/iu9/mylar-a/Marvel/Annual Owner (2023)",
            "Total": 1,
        },
    ]
    if cbr_evidence:
        comics.append(
            {
                "ComicID": "CV-810010",
                "ComicName": "Mapped CBR Series",
                "ComicYear": "2025",
                "ComicPublisher": "Fixture House",
                "ComicLocation": "/legacy/comics-b/Fixture House/Mapped CBR Series (2025)",
                "Total": len(cbr_evidence),
            }
        )

    def issue_row(
        issue_id: int | str,
        comic_id: str,
        comic_name: str,
        number: str,
        location: str,
        *,
        title: str,
        date: str,
        int_number: int,
    ) -> dict[str, object]:
        return {
            "IssueID": str(issue_id),
            "ComicID": comic_id,
            "ComicName": comic_name,
            "IssueName": title,
            "Issue_Number": number,
            "Location": location,
            "IssueDate": date,
            "Int_IssueNumber": int_number,
        }

    issues = [
        issue_row(
            8_100_010_01,
            "810001",
            "Identity Series",
            "1",
            identity_one.name,
            title="First Identity",
            date="2020-01-01",
            int_number=1000,
        ),
        issue_row(
            8_100_010_02,
            "810001",
            "Identity Series",
            "2",
            identity_two.name,
            title="Second Identity",
            date="2020-02-01",
            int_number=2000,
        ),
        issue_row(
            8_100_020_01,
            "810002",
            "Mapped Series",
            "1",
            mapped_one.name,
            title="Mapped",
            date="2021-01-01",
            int_number=1000,
        ),
        issue_row(
            8_100_030_00,
            "810003",
            "No Location Series",
            "0",
            "/iu9/mylar-b/No Location Series (2017)/No Location Series 000 (2017).cbz",
            title="Found by Absolute Issue Path",
            date="2017-01-01",
            int_number=0,
        ),
        issue_row(
            8_100_040_01,
            "810004",
            "Split Series",
            "1",
            split_one.name,
            title="Root A",
            date="2022-01-01",
            int_number=1000,
        ),
        issue_row(
            8_100_040_02,
            "810004",
            "Split Series",
            "2",
            "/iu9/mylar-b/Split Overflow/Split Series 002 (2022).cbz",
            title="Root B",
            date="2022-02-01",
            int_number=2000,
        ),
    ]
    issue_int_values = {
        "0": 0,
        ".5": 500,
        "0.5": 500,
        "1/2": 500,
        "1A": 1001,
        "10000": 10_000_000,
        "1000000": 1_000_000_000,
    }
    for index, number in enumerate(number_values, start=1):
        issues.append(
            issue_row(
                8_100_050_00 + index,
                "810005",
                "Number Lab",
                number,
                number_files[number].name,
                title=f"Number {number}",
                date="2026-01-01",
                int_number=issue_int_values[number],
            )
        )
    issues.extend(
        (
            issue_row(
                8_100_060_01,
                "810006",
                "Missing Root",
                "1",
                "Missing Root 001 (2018).cbz",
                title="Offline",
                date="2018-01-01",
                int_number=1000,
            ),
            issue_row(
                "not-an-issue-id",
                "not-a-comic-id",
                "Malformed Identity",
                "???",
                "../outside-root.cbz",
                title="Malformed",
                date="not-a-date",
                int_number=0,
            ),
            issue_row(
                8_100_090_01,
                "810009",
                "Annual Owner",
                "1",
                annual_regular.name,
                title="Regular",
                date="2023-01-01",
                int_number=1000,
            ),
        )
    )
    for index, item in enumerate(cbr_evidence, start=1):
        issues.append(
            issue_row(
                810010000 + index,
                "810010",
                "Mapped CBR Series",
                str(index),
                item.destination.name,
                title=f"Genuine {item.archive_format.upper()} Source",
                date=f"2025-{index:02d}-01",
                int_number=index * 1000,
            )
        )
    annuals = [
        {
            "IssueID": "910009001",
            "Issue_Number": "1",
            "IssueName": "Annual Event",
            "IssueDate": "2024-06-01",
            "ComicID": "810009",
            "Location": annual_file.name,
            "Int_IssueNumber": 1000,
            "ComicName": "Annual Owner",
            "ReleaseComicID": "CV-910009",
            "ReleaseComicName": "Annual Owner Annual",
        }
    ]

    arc_dir = story_arc_root / "Fixture Crisis"
    arc_dir.mkdir(parents=True)
    arc_identity = arc_dir / "01 - Identity Series 001.cbz"
    arc_mapped = arc_dir / "02 - Mapped Series 001.cbz"
    shutil.copyfile(identity_one, arc_identity)
    shutil.copyfile(mapped_one, arc_mapped)
    arc_identity.chmod(0o644)
    arc_mapped.chmod(0o644)
    story_arcs = [
        {
            "StoryArcID": "fixture-arc-1",
            "ComicName": "Identity Series",
            "IssueNumber": "1",
            "SeriesYear": "2020",
            "IssueYEAR": "2020",
            "StoryArc": "Fixture Crisis",
            "TotalIssues": "3",
            "Status": "Downloaded",
            "Location": "/iu9/story-arcs/Fixture Crisis/01 - Identity Series 001.cbz",
            "IssueArcID": "fixture-arc-entry-1",
            "ReadingOrder": 1,
            "IssueID": "810001001",
            "ComicID": "810001",
            "ReleaseDate": "2020-01-01",
            "IssueDate": "2020-01-01",
            "Publisher": "DC Comics",
            "IssuePublisher": "DC Comics",
            "IssueName": "First Identity",
            "CV_ArcID": "4045-99001",
            "Int_IssueNumber": 1000,
            "Manual": "added",
            "DateAdded": "2026-01-01",
            "DigitalDate": "2020-01-01",
            "Type": "Comic",
        },
        {
            "StoryArcID": "fixture-arc-1",
            "ComicName": "Mapped Series",
            "IssueNumber": "1",
            "SeriesYear": "2021",
            "IssueYEAR": "2021",
            "StoryArc": "Fixture Crisis",
            "TotalIssues": "3",
            "Status": "Downloaded",
            "Location": "/iu9/story-arcs/Fixture Crisis/02 - Mapped Series 001.cbz",
            "IssueArcID": "fixture-arc-entry-2",
            "ReadingOrder": 2,
            "IssueID": "810002001",
            "ComicID": "810002",
            "IssueName": "Mapped",
            "CV_ArcID": "4045-99001",
        },
        {
            "StoryArcID": "fixture-arc-1",
            "ComicName": "Unresolved Series",
            "IssueNumber": "1/2",
            "SeriesYear": "2019",
            "StoryArc": "Fixture Crisis",
            "TotalIssues": "3",
            "Status": "Wanted",
            "Location": "/iu9/story-arcs/Fixture Crisis/03 - Missing 001.cbz",
            "IssueArcID": "fixture-arc-entry-missing",
            "ReadingOrder": 3,
            "IssueName": "Missing Chapter",
            "CV_ArcID": "4045-99001",
            "Manual": "added",
        },
        {
            "StoryArcID": "fixture-arc-2",
            "ComicName": "Number Lab",
            "IssueNumber": "1000000",
            "SeriesYear": "2026",
            "StoryArc": "Duplicate Order Arc",
            "Status": "Downloaded",
            "IssueArcID": "fixture-arc-2-entry-1",
            "ReadingOrder": 7,
            "IssueID": "810005007",
            "ComicID": "810005",
            "CV_ArcID": "4045-99002",
        },
        {
            "StoryArcID": "fixture-arc-2",
            "ComicName": "Number Lab",
            "IssueNumber": "0.5",
            "SeriesYear": "2026",
            "StoryArc": "Duplicate Order Arc",
            "Status": "Downloaded",
            "IssueArcID": "fixture-arc-2-entry-2",
            "ReadingOrder": 7,
            "IssueID": "810005003",
            "ComicID": "810005",
            "CV_ArcID": "4045-99002",
        },
    ]
    readlist: list[dict[str, object]] = [
        {
            "IssueID": "810001001",
            "ComicName": "Identity Series",
            "Issue_Number": "1",
            "Status": "Downloaded",
            "DateAdded": "2026-01-01",
            "Location": (
                "/iu9/mylar-a/DC Comics/Identity Series (2020)/Identity Series 001 (2020).cbz"
            ),
            "SeriesYear": "2020",
            "ComicID": "810001",
        },
        {
            "IssueID": "missing-readlist-entry",
            "ComicName": "Unresolved Series",
            "Issue_Number": "1000000",
            "Status": "Wanted",
            "DateAdded": "2026-01-01",
            "Location": "/offline/readlist/Unresolved Series 1000000.cbz",
            "SeriesYear": "2026",
        },
    ]

    database = _create_database(
        root / "mylar.db",
        comics=comics,
        issues=issues,
        annuals=annuals,
        optional_mode="full",
        story_arcs=story_arcs,
        readlist=readlist,
    )
    variant_comics = comics[:1]
    variant_issues = issues[:2]
    absent = _create_database(
        root / "variants" / "optional-tables-absent.db",
        comics=variant_comics,
        issues=variant_issues,
        annuals=[],
        optional_mode="absent",
    )
    empty = _create_database(
        root / "variants" / "optional-tables-empty.db",
        comics=variant_comics,
        issues=variant_issues,
        annuals=[],
        optional_mode="full",
    )
    legacy = _create_database(
        root / "variants" / "legacy-storyarcs.db",
        comics=variant_comics,
        issues=variant_issues,
        annuals=[],
        optional_mode="legacy",
    )
    invalid_db = write_text(
        root / "variants" / "not-a-sqlite-database.db",
        "This is a deliberate invalid-database acceptance case.\n",
    )

    config = """[General]
READ2FILENAME = 1

[StoryArc]
STORYARCDIR = 1
STORYARC_LOCATION = /iu9/story-arcs
COPY2ARCDIR = 1
ARC_FOLDERFORMAT = $arc ($spanyears)
ARC_FILEOPS = copy
ARC_FILEOPS_SOFTLINK_RELATIVE = 0
UPCOMING_STORYARCS = 1
SEARCH_STORYARCS = 1
"""
    write_text(root / "config.ini", config)

    cases: list[dict[str, object]] = [
        {
            "id": "identity_root",
            "database": _relative(root, database),
            "series": "Identity Series",
            "expected_outcome": "success",
            "tags": ["identity-first", "exact-container-path"],
        },
        {
            "id": "explicit_path_mapping",
            "database": _relative(root, database),
            "series": "Mapped Series",
            "expected_outcome": "success",
            "tags": ["path-map", "legacy-prefix", "relative-issue-path"],
        },
        {
            "id": "missing_comic_location_absolute_issue",
            "database": _relative(root, database),
            "series": "No Location Series",
            "expected_outcome": "review",
            "tags": ["missing-comiclocation", "absolute-issue-path"],
        },
        {
            "id": "split_series",
            "database": _relative(root, database),
            "series": "Split Series",
            "expected_outcome": "review",
            "tags": ["multiple-roots", "relative-and-absolute-issue-paths"],
        },
        {
            "id": "missing_root",
            "database": _relative(root, database),
            "series": "Missing Root",
            "expected_outcome": "blocked",
            "tags": ["offline-root", "missing-file"],
        },
        {
            "id": "duplicate_comic_id",
            "database": _relative(root, database),
            "series": "Duplicate Identity",
            "expected_outcome": "review",
            "tags": ["duplicate-comic-id"],
        },
        {
            "id": "malformed_comic_id",
            "database": _relative(root, database),
            "series": "Malformed Identity",
            "expected_outcome": "review",
            "tags": ["malformed-id", "malformed-date", "root-escape-location"],
        },
        {
            "id": "annual_release_identity",
            "database": _relative(root, database),
            "series": "Annual Owner",
            "expected_outcome": "success",
            "tags": ["annual", "release-comic-id", "separate-release-series"],
        },
        {
            "id": "weird_issue_numbers",
            "database": _relative(root, database),
            "series": "Number Lab",
            "expected_outcome": "review",
            "issue_numbers": list(number_values),
            "tags": ["decimal", "fraction", "suffix", "large-number"],
        },
        {
            "id": "story_arc_full",
            "database": _relative(root, database),
            "expected_outcome": "review",
            "tags": ["storyarcs", "existing-placement", "missing-member", "duplicate-order"],
        },
        {
            "id": "readlist_full",
            "database": _relative(root, database),
            "expected_outcome": "review",
            "tags": ["readlist", "existing-member", "missing-member"],
        },
        {
            "id": "optional_tables_absent",
            "database": _relative(root, absent),
            "expected_outcome": "success",
            "tags": ["no-storyarcs-table", "no-readlist-table"],
        },
        {
            "id": "optional_tables_empty",
            "database": _relative(root, empty),
            "expected_outcome": "success",
            "tags": ["empty-storyarcs", "empty-readlist"],
        },
        {
            "id": "legacy_storyarc_table",
            "database": _relative(root, legacy),
            "expected_outcome": "review",
            "tags": ["legacy-storyarcs-columns", "legacy-readlist"],
        },
        {
            "id": "invalid_database",
            "database": _relative(root, invalid_db),
            "expected_outcome": "blocked",
            "tags": ["not-sqlite", "fail-closed"],
        },
    ]
    if cbr_evidence:
        cases.append(
            {
                "id": "mapped_genuine_cbr_conversion",
                "database": _relative(root, database),
                "series": "Mapped CBR Series",
                "paths": [_relative(root, item.destination) for item in cbr_evidence],
                "expected_outcome": "success",
                "expected_materialization": "cbz",
                "tags": [
                    "explicit-path-mapping",
                    "managed-copy",
                    "genuine-cbr",
                    "cbr-to-cbz",
                    "identity-from-mylar-database",
                ],
            }
        )
    manifest: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "fixture_kind": "iu9-mylar-import",
        "seed": seed,
        "runtime_mounts": [
            {
                "root_id": "mylar-a",
                "source_relative": "roots/library-a",
                "container_path": "/iu9/mylar-a",
            },
            {
                "root_id": "mylar-b",
                "source_relative": "roots/library-b",
                "container_path": "/iu9/mylar-b",
            },
            {
                "root_id": "story-arcs",
                "source_relative": "roots/story-arcs",
                "container_path": "/iu9/story-arcs",
            },
        ],
        "suggested_path_maps": [
            {
                "source_prefix": "/legacy/comics-b",
                "target_prefix": "/iu9/mylar-b",
            }
        ],
        "cbr_seed_set": cbr_manifest,
        "archive_identity_source": "mylar-database-with-canonical-comicinfo-where-available",
        "cases": cases,
        "tree": snapshot_tree(root),
    }
    return write_manifest(root, manifest)
