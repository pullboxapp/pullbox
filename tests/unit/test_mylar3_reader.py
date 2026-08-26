"""Unit tests for Mylar3 database reader."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from pullbox.core.exceptions import MylarReadError
from pullbox.core.mylar3_reader import Mylar3Reader
from pullbox.models.issue import IssueType
from scripts.mylar3_import_fixture import create_minimal_cbz, create_mylar3_db

if TYPE_CHECKING:
    from pathlib import Path


def _create_mylar_db(
    db_path: Path,
    series: list[dict[str, str | int | None]] | None = None,
    issues: list[dict[str, str | int | None]] | None = None,
    annuals: list[dict[str, str | int | None]] | None = None,
) -> None:
    """Create a fake Mylar3 database with a comic table."""
    create_mylar3_db(
        db_path,
        series=series or [],
        issues=issues,
        annuals=annuals,
    )


@pytest.mark.asyncio
async def test_annual_row_preserves_release_identity_and_issue_type(tmp_path: Path) -> None:
    db = tmp_path / "mylar.db"
    series_dir = tmp_path / "comics" / "X-Men"
    regular_path = series_dir / "X-Men 001 (2021).cbz"
    annual_path = series_dir / "X-Men Annual 001 (2023).cbz"
    create_minimal_cbz(regular_path)
    create_minimal_cbz(annual_path)
    _create_mylar_db(
        db,
        [
            {
                "ComicID": "CV-140553",
                "ComicName": "X-Men",
                "ComicYear": "2021",
                "ComicPublisher": "Marvel",
                "ComicLocation": str(series_dir),
                "Total": 30,
            }
        ],
        issues=[
            {
                "IssueID": "900001",
                "ComicID": "140553",
                "ComicName": "X-Men",
                "IssueName": "Fearless",
                "Issue_Number": "1",
                "Location": regular_path.name,
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
                "Location": annual_path.name,
                "IssueDate": "2023-08-16",
                "ReleaseComicID": "CV-153726",
                "ReleaseComicName": "X-Men Annual",
            }
        ],
    )

    results = await Mylar3Reader(db).read_series()

    assert len(results) == 2
    by_cv_id = {item.mylar3_cv_id: item for item in results}

    regular_series = by_cv_id[140553]
    assert regular_series.raw_series_name == "X-Men"
    assert [item.file_name for item in regular_series.files] == [regular_path.name]
    assert regular_series.files[0].issue_type == IssueType.ISSUE
    assert regular_series.files[0].comicvine_series_id == 140553

    annual_series = by_cv_id[153726]
    assert annual_series.raw_series_name == "X-Men Annual"
    assert annual_series.raw_year == 2023
    assert annual_series.raw_publisher == "Marvel"
    assert annual_series.diagnostics["source_issue_type"] == IssueType.ANNUAL.value
    assert [item.file_name for item in annual_series.files] == [annual_path.name]
    assert annual_series.files[0].issue_type == IssueType.ANNUAL
    assert annual_series.files[0].comicvine_series_id == 153726


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("file_name", "expected_type"),
    [
        ("Fixture Special 001 (2024).cbz", IssueType.SPECIAL),
        ("Fixture One-Shot 001 (2024).cbz", IssueType.ONE_SHOT),
        ("Fixture TPB 001 (2024).cbz", IssueType.TPB),
        ("Fixture Omnibus 001 (2024).cbz", IssueType.OMNIBUS),
        ("Fixture Graphic Novel 001 (2024).cbz", IssueType.GN),
    ],
)
async def test_issue_row_preserves_non_standard_filename_type(
    tmp_path: Path,
    file_name: str,
    expected_type: IssueType,
) -> None:
    db = tmp_path / "mylar.db"
    series_dir = tmp_path / "comics" / "Fixture"
    issue_path = series_dir / file_name
    create_minimal_cbz(issue_path)
    _create_mylar_db(
        db,
        [
            {
                "ComicID": "CV-200001",
                "ComicName": "Fixture",
                "ComicYear": "2024",
                "ComicLocation": str(series_dir),
                "Total": 1,
            }
        ],
        issues=[
            {
                "IssueID": "990001",
                "ComicID": "200001",
                "ComicName": "Fixture",
                "IssueName": "Fixture",
                "Issue_Number": "1",
                "Location": issue_path.name,
                "IssueDate": "2024-01-01",
            }
        ],
    )

    results = await Mylar3Reader(db).read_series()

    assert len(results) == 1
    assert results[0].files[0].issue_type == expected_type


class TestBasicRead:
    """Test 1: Basic read — 3 series, all fields populated."""

    @pytest.mark.asyncio
    async def test_reads_all_series(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        _create_mylar_db(
            db,
            [
                {
                    "ComicID": "CV-47050",
                    "ComicName": "Batman",
                    "ComicYear": "2016",
                    "ComicPublisher": "DC Comics",
                    "ComicLocation": str(tmp_path / "comics" / "Batman"),
                    "Status": "Active",
                    "Total": 85,
                },
                {
                    "ComicID": "CV-49030",
                    "ComicName": "Saga",
                    "ComicYear": "2012",
                    "ComicPublisher": "Image Comics",
                    "ComicLocation": str(tmp_path / "comics" / "Saga"),
                    "Status": "Active",
                    "Total": 54,
                },
                {
                    "ComicID": "CV-40796",
                    "ComicName": "Invincible",
                    "ComicYear": "2003",
                    "ComicPublisher": "Image Comics",
                    "ComicLocation": str(tmp_path / "comics" / "Invincible"),
                    "Status": "Ended",
                    "Total": 144,
                },
            ],
        )
        # Create comic dirs with files
        for name in ["Batman", "Saga", "Invincible"]:
            d = tmp_path / "comics" / name
            d.mkdir(parents=True)
            (d / "issue001.cbz").touch()

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert len(results) == 3
        by_name = {r.raw_series_name: r for r in results}
        assert by_name["Batman"].mylar3_cv_id == 47050
        assert by_name["Batman"].raw_year == 2016
        assert by_name["Batman"].raw_publisher == "DC Comics"
        assert by_name["Saga"].mylar3_cv_id == 49030
        assert by_name["Invincible"].mylar3_cv_id == 40796


class TestNullYear:
    """Test 2: NULL year handled gracefully."""

    @pytest.mark.asyncio
    async def test_null_year(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        _create_mylar_db(
            db,
            [
                {"ComicID": "CV-100", "ComicName": "NoYear", "ComicYear": None},
            ],
        )

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert len(results) == 1
        assert results[0].raw_year is None

    @pytest.mark.asyncio
    async def test_zero_year(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        _create_mylar_db(
            db,
            [
                {"ComicID": "CV-100", "ComicName": "ZeroYear", "ComicYear": "0000"},
            ],
        )

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert len(results) == 1
        assert results[0].raw_year is None


class TestComicVineId:
    """Test 3: ComicVine ID preserved from Mylar3 ComicID field."""

    @pytest.mark.asyncio
    async def test_cv_id_extraction(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        _create_mylar_db(
            db,
            [
                {"ComicID": "CV-47050", "ComicName": "Batman"},
            ],
        )

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert results[0].mylar3_cv_id == 47050

    @pytest.mark.asyncio
    async def test_issue_metadata_is_preserved_from_mylar3_issues(
        self,
        tmp_path: Path,
    ) -> None:
        db = tmp_path / "mylar.db"
        series_dir = tmp_path / "comics" / "Batman"
        series_dir.mkdir(parents=True)
        issue_path = series_dir / "Batman 001 (2016).cbz"
        issue_path.touch()
        _create_mylar_db(
            db,
            [
                {
                    "ComicID": "CV-47050",
                    "ComicName": "Batman",
                    "ComicYear": "2016",
                    "ComicPublisher": "DC Comics",
                    "ComicLocation": str(series_dir),
                    "Status": "Ended",
                    "Total": 85,
                },
            ],
            issues=[
                {
                    "IssueID": "500001",
                    "ComicID": "47050",
                    "ComicName": "Batman",
                    "IssueName": "I Am Gotham",
                    "Issue_Number": "1",
                    "Location": issue_path.name,
                    "IssueDate": "2016-08-01",
                },
            ],
        )

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert len(results) == 1
        series = results[0]
        assert series.diagnostics["issue_count_hint"] == 85
        assert series.diagnostics["series_status"] == "Ended"
        assert series.files[0].comicvine_issue_id == 500001
        assert series.files[0].comicvine_series_id == 47050
        assert series.files[0].issue_count_hint == 85
        assert series.files[0].series_status == "Ended"
        assert series.files[0].metadata_signals["comicvine_issue_id"] == "mylar3"
        assert series.files[0].metadata_signals["comicvine_series_id"] == "mylar3"
        assert series.files[0].metadata_diagnostics["mylar3_issue"] == {
            "issue_id": 500001,
            "issue_number": "1",
            "title": "I Am Gotham",
            "release_date": "2016-08-01",
        }


class TestStatusHandling:
    """Test 4: Import ALL series regardless of Mylar3 status."""

    @pytest.mark.asyncio
    async def test_all_statuses_imported(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        statuses = ["Active", "Ended", "Paused", "Wanted"]
        series = [
            {"ComicID": f"CV-{i}", "ComicName": f"Series{i}", "Status": s}
            for i, s in enumerate(statuses)
        ]
        _create_mylar_db(db, series)

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert len(results) == 4

    @pytest.mark.asyncio
    async def test_wanted_has_no_files(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        _create_mylar_db(
            db,
            [
                {
                    "ComicID": "CV-1",
                    "ComicName": "Wanted",
                    "Status": "Wanted",
                    "ComicLocation": None,
                },
            ],
        )

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert results[0].has_files is False


class TestMissingFiles:
    """Test 5: Series with no comic files on disk."""

    @pytest.mark.asyncio
    async def test_nonexistent_location(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        _create_mylar_db(
            db,
            [
                {
                    "ComicID": "CV-1",
                    "ComicName": "Gone",
                    "ComicLocation": str(tmp_path / "nonexistent"),
                },
            ],
        )

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert len(results) == 1
        assert results[0].file_count == 0
        assert results[0].has_files is False


class TestInvalidDatabase:
    """Test 6: Invalid database file."""

    @pytest.mark.asyncio
    async def test_not_sqlite(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "mylar.db"
        bad_file.write_text("not a database")

        reader = Mylar3Reader(bad_file)
        with pytest.raises(MylarReadError, match="read"):
            await reader.read_series()


class TestWrongDatabase:
    """Test 7: Valid SQLite but no comics table."""

    @pytest.mark.asyncio
    async def test_no_comic_table(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE other_table (id INTEGER)")
        conn.close()

        reader = Mylar3Reader(db)
        with pytest.raises(MylarReadError, match="Not a Mylar3 database"):
            await reader.read_series()


class TestLargeDatabase:
    """Test 8: Large database performance."""

    @pytest.mark.asyncio
    async def test_500_series(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        series = [
            {"ComicID": f"CV-{i}", "ComicName": f"Series {i}", "ComicYear": "2020"}
            for i in range(500)
        ]
        _create_mylar_db(db, series)

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert len(results) == 500


class TestMissingPath:
    """Test 9: Path does not exist."""

    @pytest.mark.asyncio
    async def test_file_not_found(self, tmp_path: Path) -> None:
        reader = Mylar3Reader(tmp_path / "nonexistent.db")
        with pytest.raises(FileNotFoundError):
            await reader.read_series()


class TestDuplicateCvId:
    """Test 10: Duplicate comicvine_id deduplicated."""

    @pytest.mark.asyncio
    async def test_dedup_by_cv_id(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        _create_mylar_db(
            db,
            [
                {"ComicID": "CV-47050", "ComicName": "Batman"},
                {"ComicID": "CV-47050", "ComicName": "Batman (Duplicate)"},
            ],
        )

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert len(results) == 1
        assert results[0].raw_series_name == "Batman"


class TestPathMapTranslation:
    """Test 11: Path map translates container paths to host paths."""

    @pytest.mark.asyncio
    async def test_path_map(self, tmp_path: Path) -> None:
        storage = tmp_path / "storage" / "comics" / "Batman (2016)"
        storage.mkdir(parents=True)
        for i in range(3):
            (storage / f"issue{i:03d}.cbz").touch()

        db = tmp_path / "mylar.db"
        _create_mylar_db(
            db,
            [
                {
                    "ComicID": "CV-1",
                    "ComicName": "Batman",
                    "ComicYear": "2016",
                    "ComicLocation": "/data/comics/Batman (2016)",
                },
            ],
        )

        reader = Mylar3Reader(db, path_map={"/data": str(tmp_path / "storage")})
        results = await reader.read_series()

        assert len(results) == 1
        assert results[0].file_count == 3


class TestPathMapNoMatch:
    """Test 12: Path map doesn't match — file count 0, handled gracefully."""

    @pytest.mark.asyncio
    async def test_unmatched_path_map(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        _create_mylar_db(
            db,
            [
                {"ComicID": "CV-1", "ComicName": "Batman", "ComicLocation": "/other/path/Batman"},
            ],
        )

        reader = Mylar3Reader(db, path_map={"/data": str(tmp_path / "storage")})
        results = await reader.read_series()

        assert len(results) == 1
        assert results[0].file_count == 0
        assert results[0].has_files is False


class TestMalformedCvId:
    """Edge cases for ComicVine ID parsing."""

    @pytest.mark.asyncio
    async def test_malformed_cv_prefix(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        _create_mylar_db(
            db,
            [
                {"ComicID": "CV-abc", "ComicName": "Broken"},
            ],
        )

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert len(results) == 1
        assert results[0].mylar3_cv_id is None

    @pytest.mark.asyncio
    async def test_non_cv_non_integer_id(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        _create_mylar_db(
            db,
            [
                {"ComicID": "xyz", "ComicName": "Weird"},
            ],
        )

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert len(results) == 1
        assert results[0].mylar3_cv_id is None

    @pytest.mark.asyncio
    async def test_plain_integer_id(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        _create_mylar_db(
            db,
            [
                {"ComicID": "47050", "ComicName": "PlainId"},
            ],
        )

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert len(results) == 1
        assert results[0].mylar3_cv_id == 47050


class TestMalformedYear:
    """Edge case: non-numeric year string."""

    @pytest.mark.asyncio
    async def test_non_numeric_year(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        _create_mylar_db(
            db,
            [
                {"ComicID": "CV-1", "ComicName": "BadYear", "ComicYear": "abcd"},
            ],
        )

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert len(results) == 1
        assert results[0].raw_year is None
