"""Tests for Task R-3.1 — DiscoveredFile dataclass and scanner file enumeration.

Verifies per-file metadata extraction during collection scanning.
"""

from __future__ import annotations

import time
import zipfile
from typing import TYPE_CHECKING

import py7zr
import pytest

from pullbox.core.collection_scanner import CollectionScanner, DiscoveredFile, DiscoveredSeries

if TYPE_CHECKING:
    from pathlib import Path


def _make_series_dir(root: Path, *parts: str, files: list[str] | None = None) -> None:
    folder = root.joinpath(*parts)
    folder.mkdir(parents=True, exist_ok=True)
    for f in files or []:
        (folder / f).touch()


def _make_cbz(path: Path, xml_content: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        if xml_content is not None:
            zf.writestr("ComicInfo.xml", xml_content)
        zf.writestr("page001.jpg", b"fake image")


def _make_cb7(path: Path, xml_content: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload_dir = path.parent / "cb7_payload"
    payload_dir.mkdir(exist_ok=True)
    page_path = payload_dir / "page001.jpg"
    page_path.write_bytes(b"fake image")
    with py7zr.SevenZipFile(path, "w") as archive:
        archive.write(page_path, "page001.jpg")
        if xml_content is not None:
            comicinfo_path = payload_dir / "ComicInfo.xml"
            comicinfo_path.write_text(xml_content)
            archive.write(comicinfo_path, "ComicInfo.xml")


async def _scan_all(scanner: CollectionScanner, root: Path) -> list[DiscoveredSeries]:
    results: list[DiscoveredSeries] = []
    async for series in scanner.scan(root):
        results.append(series)
    return results


class TestDiscoveredFileDataclass:
    """DiscoveredFile has all required fields."""

    def test_fields_exist(self) -> None:
        df = DiscoveredFile(
            file_path="/tmp/Batman 001.cbz",
            file_name="Batman 001.cbz",
            file_size=1024,
            file_format="cbz",
            parsed_series="Batman",
            parsed_issue_number=1.0,
            parsed_year=None,
            parsed_publisher=None,
            has_comicinfo=False,
            comicvine_issue_id=None,
            issue_number_raw="001",
        )
        assert df.file_path == "/tmp/Batman 001.cbz"
        assert df.file_name == "Batman 001.cbz"
        assert df.file_size == 1024
        assert df.file_format == "cbz"
        assert df.parsed_series == "Batman"
        assert df.parsed_issue_number == 1.0
        assert df.parsed_year is None
        assert df.parsed_publisher is None
        assert df.has_comicinfo is False
        assert df.comicvine_issue_id is None
        assert df.issue_number_raw == "001"


class TestScannerPopulatesFiles:
    """Scanning populates DiscoveredFile entries per series."""

    @pytest.mark.asyncio
    async def test_files_populated(self, tmp_path: Path) -> None:
        _make_series_dir(
            tmp_path,
            "Batman (2016)",
            files=["Batman 001.cbz", "Batman 002.cbz", "Batman 003.cbr"],
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert len(results[0].files) == 3
        for f in results[0].files:
            assert isinstance(f, DiscoveredFile)
            assert f.file_format in ("cbz", "cbr")

    @pytest.mark.asyncio
    async def test_file_paths_are_absolute(self, tmp_path: Path) -> None:
        _make_series_dir(tmp_path, "Saga (2012)", files=["Saga 001.cbz"])

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results[0].files) == 1
        assert results[0].files[0].file_path.startswith("/")


class TestFilenameParsing:
    """Filename parsing populates parsed fields on DiscoveredFile."""

    @pytest.mark.asyncio
    async def test_parsed_series_and_issue(self, tmp_path: Path) -> None:
        _make_series_dir(tmp_path, "Batman (2016)", files=["Batman 001.cbz"])

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        f = results[0].files[0]
        assert f.parsed_series is not None
        assert f.parsed_issue_number == 1.0

    @pytest.mark.asyncio
    async def test_unparseable_filename(self, tmp_path: Path) -> None:
        """Files that can't be parsed still appear with None fields."""
        _make_series_dir(tmp_path, "Batman (2016)", files=["random_name.cbz"])

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results[0].files) == 1
        f = results[0].files[0]
        assert f.file_name == "random_name.cbz"
        # May or may not parse — just confirm the file is present
        assert f.file_format == "cbz"


class TestComicInfoExtraction:
    """ComicInfo.xml extraction populates comicvine_issue_id."""

    @pytest.mark.asyncio
    async def test_comicinfo_cv_id_populated(self, tmp_path: Path) -> None:
        series_dir = tmp_path / "Batman (2016)"
        series_dir.mkdir()
        _make_cbz(
            series_dir / "Batman 001.cbz",
            "<ComicInfo>"
            "<Series>Batman</Series>"
            "<Number>1</Number>"
            "<Notes>[cvid:12345]</Notes>"
            "</ComicInfo>",
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results[0].files) == 1
        f = results[0].files[0]
        assert f.has_comicinfo is True
        assert f.comicvine_issue_id is None
        assert f.comicvine_series_id == 12345

    @pytest.mark.asyncio
    async def test_comicinfo_with_series_name(self, tmp_path: Path) -> None:
        """ComicInfo.xml with a Series tag is detected as having comicinfo."""
        series_dir = tmp_path / "Batman (2016)"
        series_dir.mkdir()
        _make_cbz(
            series_dir / "Batman 001.cbz",
            "<ComicInfo><Series>Batman</Series><Number>5</Number></ComicInfo>",
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        f = results[0].files[0]
        assert f.has_comicinfo is True
        assert f.parsed_series == "Batman"

    @pytest.mark.asyncio
    async def test_cb7_comicinfo_extraction_populates_issue_identity(self, tmp_path: Path) -> None:
        series_dir = tmp_path / "Chicken Devil (2021)"
        series_dir.mkdir()
        _make_cb7(
            series_dir / "Chicken Devil 004 (2022).cb7",
            "<ComicInfo>"
            "<Series>Chicken Devil</Series>"
            "<Number>4</Number>"
            "<Volume>2021</Volume>"
            "<Web>https://comicvine.gamespot.com/chicken-devil-4-the-chicken-is-in-the-details/4000-905404/</Web>"
            "</ComicInfo>",
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        f = results[0].files[0]
        assert f.has_comicinfo is True
        assert f.parsed_series == "Chicken Devil"
        assert f.comicvine_issue_id == 905404


class TestFileFormatDetection:
    """File format is detected from extension."""

    @pytest.mark.asyncio
    async def test_format_from_extension(self, tmp_path: Path) -> None:
        _make_series_dir(
            tmp_path,
            "Batman (2016)",
            files=["Batman 001.cbz", "Batman 002.cbr", "Batman 003.pdf"],
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        formats = {f.file_format for f in results[0].files}
        assert formats == {"cbz", "cbr", "pdf"}


class TestSamplePathsBackwardCompat:
    """sample_paths is still populated for backward compatibility."""

    @pytest.mark.asyncio
    async def test_sample_paths_still_populated(self, tmp_path: Path) -> None:
        _make_series_dir(
            tmp_path,
            "Batman (2016)",
            files=[f"Batman {i:03d}.cbz" for i in range(10)],
        )

        scanner = CollectionScanner(max_sample_paths=5)
        results = await _scan_all(scanner, tmp_path)

        assert len(results[0].sample_paths) == 5
        assert len(results[0].files) == 10


class TestLargeDirectoryPerformance:
    """1000 files completes within reasonable time."""

    @pytest.mark.asyncio
    async def test_1000_files_performance(self, tmp_path: Path) -> None:
        series_dir = tmp_path / "Big Series (2020)"
        series_dir.mkdir()
        for i in range(1000):
            (series_dir / f"Issue {i:04d}.cbz").touch()

        scanner = CollectionScanner()
        start = time.monotonic()
        results = await _scan_all(scanner, tmp_path)
        elapsed = time.monotonic() - start

        assert len(results) == 1
        assert results[0].file_count == 1000
        assert len(results[0].files) == 1000
        assert elapsed < 30

    @pytest.mark.asyncio
    async def test_generic_issue_filenames_collapse_to_folder_series(self, tmp_path: Path) -> None:
        series_dir = tmp_path / "Big Series (2020)"
        series_dir.mkdir()
        for i in range(25):
            (series_dir / f"Issue {i:04d}.cbz").touch()

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == "Big Series"
        assert results[0].file_count == 25


def _touch(path: Path) -> None:
    """Create an empty file, ensuring parent dirs exist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


class TestFileEdgeCases:
    """Edge case tests for DiscoveredFile population during scans."""

    @pytest.mark.asyncio
    async def test_mixed_case_extension_in_discovered_file(self, tmp_path: Path) -> None:
        """'.CBZ' file has file_format='cbz' (lowercased)."""
        _make_series_dir(tmp_path, "Batman (2016)", files=["Batman 001.CBZ"])

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert len(results[0].files) == 1
        assert results[0].files[0].file_format == "cbz"

    @pytest.mark.asyncio
    async def test_zero_byte_file(self, tmp_path: Path) -> None:
        """Empty file (0 bytes) still gets scanned with file_size=0."""
        _make_series_dir(tmp_path, "Batman (2016)", files=["Batman 001.cbz"])

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results[0].files) == 1
        assert results[0].files[0].file_size == 0

    @pytest.mark.asyncio
    async def test_file_size_accuracy(self, tmp_path: Path) -> None:
        """Write specific bytes to a file, verify file_size matches exactly."""
        series_dir = tmp_path / "Batman (2016)"
        series_dir.mkdir()
        test_file = series_dir / "Batman 001.cbz"
        content = b"x" * 12345
        test_file.write_bytes(content)

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results[0].files) == 1
        assert results[0].files[0].file_size == 12345

    @pytest.mark.asyncio
    async def test_issue_number_raw_preserves_embedded_identity(self, tmp_path: Path) -> None:
        """Keep the embedded issue identity instead of replacing it with the filename."""
        series_dir = tmp_path / "Batman (2016)"
        series_dir.mkdir()
        _make_cbz(
            series_dir / "Batman 001.cbz",
            "<ComicInfo><Series>Batman</Series><Number>5</Number></ComicInfo>",
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        f = results[0].files[0]
        assert f.issue_number_raw == "5"

    @pytest.mark.asyncio
    async def test_fractional_issue_number_raw(self, tmp_path: Path) -> None:
        """File 'Batman 001.5.cbz' yields issue_number_raw='1.5' (not '1')."""
        _make_series_dir(tmp_path, "Batman (2016)", files=["Batman 001.5.cbz"])

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results[0].files) == 1
        f = results[0].files[0]
        assert f.parsed_issue_number == 1.5
        assert f.issue_number_raw == "1.5"

    @pytest.mark.asyncio
    async def test_cb7_extension_detected(self, tmp_path: Path) -> None:
        """.cb7 file has file_format='cb7'."""
        _make_series_dir(tmp_path, "Batman (2016)", files=["Batman 001.cb7"])

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results[0].files) == 1
        assert results[0].files[0].file_format == "cb7"

    @pytest.mark.asyncio
    async def test_epub_extension_detected(self, tmp_path: Path) -> None:
        """.epub file has file_format='epub'."""
        _make_series_dir(tmp_path, "Batman (2016)", files=["Batman 001.epub"])

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results[0].files) == 1
        assert results[0].files[0].file_format == "epub"

    @pytest.mark.asyncio
    async def test_corrupted_cbz_no_crash(self, tmp_path: Path) -> None:
        """A non-zip file with .cbz extension doesn't crash the scanner."""
        series_dir = tmp_path / "Batman (2016)"
        series_dir.mkdir()
        corrupted = series_dir / "Batman 001.cbz"
        corrupted.write_bytes(b"this is not a valid zip archive at all")

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert len(results[0].files) == 1
        f = results[0].files[0]
        assert f.file_format == "cbz"
        # ComicInfo extraction should gracefully fail
        assert f.has_comicinfo is False
        assert f.comicvine_issue_id is None
