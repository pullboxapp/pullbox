"""Deterministic Mylar3 database and comic fixtures for tests and benchmarks."""

from __future__ import annotations

import sqlite3
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


MylarRow = Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Mylar3ScaleFixture:
    """Generated fixture paths and expected import totals."""

    db_path: Path
    source_series_count: int
    discovered_series_count: int
    file_count: int
    annual_count: int


def create_minimal_cbz(path: Path) -> None:
    """Create a tiny valid CBZ without provider-derived metadata."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("001.jpg", b"pullbox-mylar-fixture")


def create_mylar3_db(
    db_path: Path,
    *,
    series: Sequence[MylarRow] = (),
    issues: Sequence[MylarRow] | None = None,
    annuals: Sequence[MylarRow] | None = None,
) -> None:
    """Create the Mylar tables used by Pullbox's migration reader."""
    connection = sqlite3.connect(db_path)
    try:
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
                for row in series
            ],
        )

        if issues is not None:
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

        if annuals is not None:
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
        connection.commit()
    finally:
        connection.close()


def create_scaled_mylar3_fixture(
    root: Path,
    *,
    series_count: int = 463,
    files_per_series: int = 5,
    annual_count: int = 47,
) -> Mylar3ScaleFixture:
    """Create a realistic large Mylar library with regular/Annual collisions."""
    if series_count < 1 or files_per_series < 2:
        raise ValueError("Scale fixtures require at least one series and two files per series")
    annual_count = min(max(annual_count, 0), series_count)
    comics_root = root / "comics"
    series_rows: list[dict[str, object]] = []
    issue_rows: list[dict[str, object]] = []
    annual_rows: list[dict[str, object]] = []

    for series_index in range(series_count):
        series_cv_id = 100_000 + series_index
        title = f"Fixture Series {series_index:04d}"
        year = 2000 + (series_index % 25)
        series_dir = comics_root / f"{title} ({year})"
        has_annual = series_index < annual_count
        regular_issue_count = files_per_series - 1 if has_annual else files_per_series
        series_rows.append(
            {
                "ComicID": f"CV-{series_cv_id}",
                "ComicName": title,
                "ComicYear": str(year),
                "ComicPublisher": "Fixture Comics",
                "ComicLocation": str(series_dir),
                "Status": "Active",
                "Total": regular_issue_count,
            }
        )
        for issue_number in range(1, regular_issue_count + 1):
            issue_path = series_dir / f"{title} {issue_number:03d} ({year}).cbz"
            create_minimal_cbz(issue_path)
            issue_rows.append(
                {
                    "IssueID": str((series_cv_id * 1000) + issue_number),
                    "ComicID": str(series_cv_id),
                    "ComicName": title,
                    "IssueName": f"Issue {issue_number}",
                    "Issue_Number": str(issue_number),
                    "Location": issue_path.name,
                    "IssueDate": f"{year}-01-01",
                }
            )

        if has_annual:
            release_cv_id = 900_000 + series_index
            annual_path = series_dir / f"{title} Annual 001 ({year}).cbz"
            create_minimal_cbz(annual_path)
            annual_rows.append(
                {
                    "IssueID": str(800_000_000 + series_index),
                    "ComicID": str(series_cv_id),
                    "ComicName": title,
                    "IssueName": "Annual",
                    "Issue_Number": "1",
                    "Location": annual_path.name,
                    "IssueDate": f"{year}-06-01",
                    "ReleaseComicID": f"CV-{release_cv_id}",
                    "ReleaseComicName": f"{title} Annual",
                }
            )

    db_path = root / "mylar.db"
    create_mylar3_db(
        db_path,
        series=series_rows,
        issues=issue_rows,
        annuals=annual_rows,
    )
    return Mylar3ScaleFixture(
        db_path=db_path,
        source_series_count=series_count,
        discovered_series_count=series_count + annual_count,
        file_count=series_count * files_per_series,
        annual_count=annual_count,
    )
