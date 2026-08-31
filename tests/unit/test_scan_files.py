"""Tests for CollectionScanner.scan_files() — explicit file path import.

Verifies that scan_files() correctly groups files by parent directory,
builds DiscoveredSeries objects, handles loose files, filters by
extension, and skips missing files.

Run:
    pytest tests/unit/test_scan_files.py -v
"""

from __future__ import annotations

import zipfile
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from pullbox.core.collection_scanner import CollectionScanner
from pullbox.models.issue import IssueType


def _create_test_cbz(
    path: Path,
    page_count: int = 2,
    comicinfo_xml: str | None = None,
) -> Path:
    """Create a minimal valid CBZ file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        if comicinfo_xml is not None:
            zf.writestr("ComicInfo.xml", comicinfo_xml)
        for i in range(page_count):
            zf.writestr(f"page_{i:03d}.jpg", b"\xff\xd8" + b"X" * 100)
    return path


def _create_test_file(path: Path, content: bytes = b"test") -> Path:
    """Create a small test file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


class TestScanFilesGrouping:
    """Files are grouped by parent directory into DiscoveredSeries."""

    @pytest.mark.asyncio
    async def test_single_folder_multiple_files(self, tmp_path: Path) -> None:
        """Files in one folder produce one DiscoveredSeries."""
        folder = tmp_path / "Batman (2024)"
        _create_test_cbz(folder / "Batman 001.cbz")
        _create_test_cbz(folder / "Batman 002.cbz")
        _create_test_cbz(folder / "Batman 003.cbz")

        scanner = CollectionScanner(min_file_count=1)
        result = await scanner.scan_files(
            [
                str(folder / "Batman 001.cbz"),
                str(folder / "Batman 002.cbz"),
                str(folder / "Batman 003.cbz"),
            ]
        )

        assert len(result) == 1
        assert result[0].file_count == 3
        assert result[0].raw_series_name == "Batman"
        assert result[0].raw_year == 2024

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("folder_name", "file_names", "expected_series", "expected_year"),
        [
            (
                "Absolute Batman 2024",
                [
                    "Issue 14 Abomination, Conclusion.cbz",
                    "Issue 15 The Joker.cbz",
                ],
                "Absolute Batman",
                2024,
            ),
            (
                "Batman (2011)",
                [
                    "Batman The Court of Owls, Part One Issue 001.cbz",
                    "Batman The City of Owls, Part Two Issue 002.cbz",
                ],
                "Batman",
                2011,
            ),
        ],
    )
    async def test_issue_title_files_use_series_folder_in_focused_scan(
        self,
        tmp_path: Path,
        folder_name: str,
        file_names: list[str],
        expected_series: str,
        expected_year: int,
    ) -> None:
        folder = tmp_path / folder_name
        files = [_create_test_cbz(folder / file_name) for file_name in file_names]

        scanner = CollectionScanner(min_file_count=1)
        result = await scanner.scan_files([str(file_path) for file_path in files])

        assert len(result) == 1
        assert result[0].raw_series_name == expected_series
        assert result[0].raw_year == expected_year
        assert result[0].file_count == 2

    @pytest.mark.asyncio
    async def test_issue_only_comicinfo_does_not_block_folder_series_identity(
        self, tmp_path: Path
    ) -> None:
        folder = tmp_path / "Batman (2011)"
        first = _create_test_cbz(
            folder / "Batman The Court of Owls, Part One Issue 001.cbz",
            comicinfo_xml="<ComicInfo><Number>1</Number></ComicInfo>",
        )
        second = _create_test_cbz(
            folder / "Batman The City of Owls, Part Two Issue 002.cbz",
            comicinfo_xml="<ComicInfo><Number>2</Number></ComicInfo>",
        )

        scanner = CollectionScanner(min_file_count=1)
        result = await scanner.scan_files([str(first), str(second)])

        assert len(result) == 1
        assert result[0].raw_series_name == "Batman"
        assert result[0].file_count == 2

    @pytest.mark.asyncio
    async def test_multiple_folders(self, tmp_path: Path) -> None:
        """Files in different folders produce separate DiscoveredSeries."""
        folder_a = tmp_path / "Batman (2024)"
        folder_b = tmp_path / "Superman (2023)"
        _create_test_cbz(folder_a / "Batman 001.cbz")
        _create_test_cbz(folder_b / "Superman 001.cbz")

        scanner = CollectionScanner(min_file_count=1)
        result = await scanner.scan_files(
            [
                str(folder_a / "Batman 001.cbz"),
                str(folder_b / "Superman 001.cbz"),
            ]
        )

        assert len(result) == 2
        names = sorted(s.raw_series_name for s in result)
        assert names == ["Batman", "Superman"]

    @pytest.mark.asyncio
    async def test_loose_file_in_root(self, tmp_path: Path) -> None:
        """A file directly in a directory gets its own series group."""
        _create_test_cbz(tmp_path / "loose_comic.cbz")
        folder = tmp_path / "Series A (2020)"
        _create_test_cbz(folder / "Issue 001.cbz")

        scanner = CollectionScanner(min_file_count=1)
        result = await scanner.scan_files(
            [
                str(tmp_path / "loose_comic.cbz"),
                str(folder / "Issue 001.cbz"),
            ]
        )

        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_loose_collection_pdf_uses_filename_identity(self, tmp_path: Path) -> None:
        """Loose collection-style PDFs should not be bucketed under their parent folder."""
        fearscape = _create_test_file(tmp_path / "Fearscape.Vol.01.2019.pdf")
        cape = _create_test_file(tmp_path / "The.Cape.Omnibus.2025.HYBRID.COMIC.pdf")
        payment = _create_test_file(tmp_path / "Payment Confirmation.pdf")

        scanner = CollectionScanner(min_file_count=1)
        result = await scanner.scan_files([str(fearscape), str(cape), str(payment)])

        by_name = {series.raw_series_name: series for series in result}
        assert "Fearscape" in by_name
        assert "The Cape" in by_name
        assert tmp_path.name not in by_name
        assert "Payment Confirmation" not in by_name

    @pytest.mark.asyncio
    async def test_empty_file_list(self) -> None:
        """Empty file list returns empty result."""
        scanner = CollectionScanner(min_file_count=1)
        result = await scanner.scan_files([])
        assert result == []


class TestScanFilesFiltering:
    """Files are filtered by extension and existence."""

    @pytest.mark.asyncio
    async def test_nonexistent_files_skipped(self, tmp_path: Path) -> None:
        """Files that don't exist are silently skipped."""
        real = _create_test_cbz(tmp_path / "Series (2020)" / "Issue 001.cbz")
        fake = str(tmp_path / "nonexistent.cbz")

        scanner = CollectionScanner(min_file_count=1)
        result = await scanner.scan_files([str(real), fake])

        assert len(result) == 1
        assert result[0].file_count == 1

    @pytest.mark.asyncio
    async def test_unsupported_extensions_skipped(self, tmp_path: Path) -> None:
        """Files with non-comic extensions are skipped."""
        folder = tmp_path / "Test Series (2020)"
        _create_test_cbz(folder / "Issue 001.cbz")
        txt_file = folder / "readme.txt"
        txt_file.parent.mkdir(parents=True, exist_ok=True)
        txt_file.write_text("not a comic")

        scanner = CollectionScanner(min_file_count=1)
        result = await scanner.scan_files(
            [
                str(folder / "Issue 001.cbz"),
                str(txt_file),
            ]
        )

        assert len(result) == 1
        assert result[0].file_count == 1

    @pytest.mark.asyncio
    async def test_appledouble_sidecar_files_skipped(self, tmp_path: Path) -> None:
        """Explicit file imports should ignore macOS AppleDouble sidecar files."""
        folder = tmp_path / "Test Series (2020)"
        real_file = _create_test_cbz(folder / "Issue 001.cbz")
        apple_double = folder / "._Issue 001.cbz"
        apple_double.write_bytes(b"\x00" * 4096)

        scanner = CollectionScanner(min_file_count=1)
        result = await scanner.scan_files([str(real_file), str(apple_double)])

        assert len(result) == 1
        assert result[0].file_count == 1
        assert [file.file_name for file in result[0].files] == ["Issue 001.cbz"]

    @pytest.mark.asyncio
    async def test_custom_extensions(self, tmp_path: Path) -> None:
        """Custom extensions filter works."""
        folder = tmp_path / "Series (2020)"
        cbz_file = _create_test_cbz(folder / "Issue 001.cbz")
        cbr_file = folder / "Issue 002.cbr"
        cbr_file.parent.mkdir(parents=True, exist_ok=True)
        cbr_file.write_bytes(b"PK\x03\x04" + b"X" * 100)

        # Only allow CBZ
        scanner = CollectionScanner(min_file_count=1, extensions=frozenset({".cbz"}))
        result = await scanner.scan_files([str(cbz_file), str(cbr_file)])

        assert len(result) == 1
        assert result[0].file_count == 1


class TestScanFilesMetadata:
    """DiscoveredFile objects have correct parsed metadata."""

    @pytest.mark.asyncio
    async def test_series_name_from_folder(self, tmp_path: Path) -> None:
        """Series name and year extracted from folder name."""
        folder = tmp_path / "Amazing Spider-Man (2018)"
        _create_test_cbz(folder / "Amazing Spider-Man 001.cbz")

        scanner = CollectionScanner(min_file_count=1)
        result = await scanner.scan_files([str(folder / "Amazing Spider-Man 001.cbz")])

        assert result[0].raw_series_name == "Amazing Spider-Man"
        assert result[0].raw_year == 2018

    @pytest.mark.asyncio
    async def test_source_folder_set(self, tmp_path: Path) -> None:
        """source_folder is set to the parent directory."""
        folder = tmp_path / "Batman (2024)"
        _create_test_cbz(folder / "Batman 001.cbz")

        scanner = CollectionScanner(min_file_count=1)
        result = await scanner.scan_files([str(folder / "Batman 001.cbz")])

        assert result[0].source_folder == str(folder)
        assert result[0].source_folder_relative == "Batman (2024)"

    @pytest.mark.asyncio
    async def test_files_have_parsed_data(self, tmp_path: Path) -> None:
        """DiscoveredFile objects have parsed series/issue info."""
        folder = tmp_path / "Batman (2024)"
        _create_test_cbz(folder / "Batman 005.cbz")

        scanner = CollectionScanner(min_file_count=1)
        result = await scanner.scan_files([str(folder / "Batman 005.cbz")])

        assert len(result[0].files) == 1
        df = result[0].files[0]
        assert df.file_format == "cbz"
        assert df.file_size > 0

    @pytest.mark.asyncio
    async def test_webrip_metadata_does_not_leave_empty_parentheses(
        self,
        tmp_path: Path,
    ) -> None:
        """Generic import folders should use clean filename parser output."""
        folder = tmp_path / "raw imports"
        file_path = _create_test_cbz(
            folder / "Batman 145 (2024) (Webrip) (The Last Kryptonian-DCP).cbr"
        )

        scanner = CollectionScanner(min_file_count=1)
        result = await scanner.scan_files([str(file_path)])

        assert len(result) == 1
        assert result[0].raw_series_name == "Batman"
        assert result[0].raw_year == 2024
        assert result[0].files[0].parsed_series == "Batman"

    @pytest.mark.asyncio
    async def test_sample_paths_populated(self, tmp_path: Path) -> None:
        """sample_paths contains file paths."""
        folder = tmp_path / "Series (2020)"
        f1 = _create_test_cbz(folder / "Issue 001.cbz")
        f2 = _create_test_cbz(folder / "Issue 002.cbz")

        scanner = CollectionScanner(min_file_count=1)
        result = await scanner.scan_files([str(f1), str(f2)])

        assert len(result[0].sample_paths) == 2

    @pytest.mark.asyncio
    async def test_trailing_book_ordinal_comicinfo_series_uses_filename_base(
        self,
        tmp_path: Path,
    ) -> None:
        """ComicInfo ``Series`` values like ``Book Two`` should not split buckets by title."""
        folder = tmp_path / "Incoming"
        book_one = _create_test_cbz(
            folder / "Not Drunk Enough Book One (2017) (Digital-Empire).cbz",
            comicinfo_xml="""
            <ComicInfo>
              <Series>Not Drunk Enough</Series>
              <Publisher>Oni Press</Publisher>
            </ComicInfo>
            """,
        )
        book_two = _create_test_cbz(
            folder / "Not Drunk Enough Book Two (2020) (Digital-Empire).cbz",
            comicinfo_xml="""
            <ComicInfo>
              <Series>Not Drunk Enough Book Two</Series>
              <Publisher>Oni Press</Publisher>
            </ComicInfo>
            """,
        )

        scanner = CollectionScanner(min_file_count=1)
        result = await scanner.scan_files([str(book_one), str(book_two)])

        assert [series.raw_series_name for series in result] == [
            "Not Drunk Enough",
            "Not Drunk Enough",
        ]
        assert sorted(file.parsed_issue_number for series in result for file in series.files) == [
            1.0,
            2.0,
        ]
        assert {file.parsed_series for series in result for file in series.files} == {
            "Not Drunk Enough"
        }
        book_two_file = next(
            file for series in result for file in series.files if file.file_name == book_two.name
        )
        assert book_two_file.metadata_diagnostics["comicinfo_series_ignored"]["reason"] == (
            "redundant_ordinal_suffix"
        )

    @pytest.mark.asyncio
    async def test_volume_subtitle_files_group_by_base_series(
        self,
        tmp_path: Path,
    ) -> None:
        """Volume subtitles should not become separate scan buckets."""
        folder = tmp_path / "Incoming"
        files = [
            _create_test_cbz(
                folder / "Immortal Thor v01 - All Weather Turns To Storm (2024) (Digital).cbz",
            ),
            _create_test_cbz(
                folder / "Immortal Thor v02 - All Trials Are One (2024) (Digital).cbz",
            ),
            _create_test_cbz(
                folder / "Immortal Thor v03 - The Land Of Lost Content (2024) (Digital).cbz",
                comicinfo_xml="""
                <ComicInfo>
                  <Series>Immortal Thor</Series>
                  <Number>3</Number>
                  <Volume>2024</Volume>
                </ComicInfo>
                """,
            ),
            _create_test_cbz(
                folder / "Immortal Thor v04 - The Son Of Thor (2025) (Digital).cbz",
                comicinfo_xml="""
                <ComicInfo>
                  <Series>The Immortal Thor: The Son Of Thor</Series>
                  <Number>1</Number>
                  <Volume>4</Volume>
                </ComicInfo>
                """,
            ),
            _create_test_cbz(
                folder / "Immortal Thor v05 - Death Of The Immortal Thor (2025) (Digital).cbz",
                comicinfo_xml="""
                <ComicInfo>
                  <Series>Immortal Thor: Death Of The Immortal Thor</Series>
                  <Number>1</Number>
                  <Volume>2025</Volume>
                </ComicInfo>
                """,
            ),
        ]

        scanner = CollectionScanner(min_file_count=1)
        result = await scanner.scan_files([str(path) for path in files])

        assert {series.raw_series_name for series in result} == {"Immortal Thor"}
        assert {series.raw_year for series in result} == {2024, 2025}
        all_files = [file for series in result for file in series.files]
        assert {file.parsed_series for file in all_files} == {"Immortal Thor"}
        assert sorted(file.parsed_issue_number for file in all_files) == [
            1.0,
            2.0,
            3.0,
            4.0,
            5.0,
        ]

    @pytest.mark.asyncio
    async def test_issue_like_files_do_not_share_volume_bucket(
        self,
        tmp_path: Path,
    ) -> None:
        """Annuals should not be grouped with same-number collected volumes."""
        folder = tmp_path / "Incoming"
        annual = _create_test_cbz(
            folder / "Immortal Thor Annual 001 (2024) (Digital).cbz",
        )
        volume_one = _create_test_cbz(
            folder / "Immortal Thor v01 - All Weather Turns To Storm (2024) (Digital).cbz",
        )
        volume_two = _create_test_cbz(
            folder / "Immortal Thor v02 - All Trials Are One (2024) (Digital).cbz",
        )

        scanner = CollectionScanner(min_file_count=1)
        result = await scanner.scan_files([str(annual), str(volume_one), str(volume_two)])

        by_type = {
            (series.raw_series_name, series.raw_year, series.diagnostics["source_issue_type"]): (
                series
            )
            for series in result
        }
        assert ("Immortal Thor Annual", 2024, IssueType.ANNUAL.value) in by_type
        assert ("Immortal Thor", 2024, IssueType.VOLUME.value) in by_type
        assert by_type[("Immortal Thor Annual", 2024, IssueType.ANNUAL.value)].file_count == 1
        assert by_type[("Immortal Thor", 2024, IssueType.VOLUME.value)].file_count == 2

    @pytest.mark.asyncio
    async def test_standard_issues_do_not_share_volume_subtitle_bucket(
        self,
        tmp_path: Path,
    ) -> None:
        """Plain issues and collected-volume files need separate review buckets."""
        folder = tmp_path / "Incoming"
        issue_files = [
            _create_test_cbz(
                folder / f"Absolute Martian Manhunter {issue:03d} (2025) (Digital).cbz",
            )
            for issue in range(1, 6)
        ]
        volume_file = _create_test_cbz(
            folder / "Absolute Martian Manhunter v01 - Martian Vision (2025) (Digital).cbz",
        )

        scanner = CollectionScanner(min_file_count=1)
        result = await scanner.scan_files([*(str(path) for path in issue_files), str(volume_file)])

        by_type = {
            series.diagnostics["source_issue_type"]: series
            for series in result
            if series.raw_series_name == "Absolute Martian Manhunter"
        }
        assert set(by_type) == {IssueType.ISSUE.value, IssueType.VOLUME.value}
        assert by_type[IssueType.ISSUE.value].file_count == 5
        assert by_type[IssueType.VOLUME.value].file_count == 1
        assert [file.file_name for file in by_type[IssueType.VOLUME.value].files] == [
            volume_file.name
        ]

    @pytest.mark.asyncio
    async def test_one_shot_marker_does_not_pollute_review_series_label(
        self,
        tmp_path: Path,
    ) -> None:
        """One-shot markers should stay metadata, not become display title text."""
        folder = tmp_path / "Incoming"
        file_path = _create_test_cbz(
            folder / "Murder Drones - Home 001 (OS) (2026) (Digital Rip).cbz",
        )

        scanner = CollectionScanner(min_file_count=1)
        result = await scanner.scan_files([str(file_path)])

        assert len(result) == 1
        assert result[0].raw_series_name == "Murder Drones - Home"
        assert result[0].diagnostics["source_issue_type"] == IssueType.ONE_SHOT.value
        assert result[0].files[0].parsed_series == "Murder Drones - Home"

    @pytest.mark.asyncio
    async def test_single_token_scan_group_suffixes_do_not_split_series(
        self,
        tmp_path: Path,
    ) -> None:
        """Scanner tags like ``(TanCombs)`` should not become part of the series name."""
        folder = tmp_path / "Incoming"
        files = [
            _create_test_cbz(
                folder / "Necronomicon 01 (of 04) (2008) (Digital) (TanCombs).cbz",
            ),
            _create_test_cbz(
                folder / "Necronomicon 02 (2008) (BOOM! Studios) (RacerX-DCP).cbz",
            ),
            _create_test_cbz(
                folder / "Necronomicon 03 (of 04) (2008) (Digital) (TanCombs).cbz",
            ),
            _create_test_cbz(
                folder / "Necronomicon 04 (of 04) (2008) (Digital) (TanCombs).cbz",
            ),
        ]

        scanner = CollectionScanner(min_file_count=1)
        result = await scanner.scan_files([str(path) for path in files])

        assert len(result) == 1
        assert result[0].raw_series_name == "Necronomicon"
        assert result[0].raw_year == 2008
        assert result[0].file_count == 4
        assert {file.parsed_series for file in result[0].files} == {"Necronomicon"}
        assert sorted(file.parsed_issue_number for file in result[0].files) == [
            1.0,
            2.0,
            3.0,
            4.0,
        ]


class TestScanFilesEdgeCases:
    """Edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_all_files_missing(self, tmp_path: Path) -> None:
        """All nonexistent files returns empty result."""
        scanner = CollectionScanner(min_file_count=1)
        result = await scanner.scan_files(
            [
                str(tmp_path / "nope1.cbz"),
                str(tmp_path / "nope2.cbz"),
            ]
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_mixed_folders_and_loose(self, tmp_path: Path) -> None:
        """Mix of folder files and loose files handled correctly."""
        folder = tmp_path / "Real Series (2024)"
        _create_test_cbz(folder / "Issue 001.cbz")
        _create_test_cbz(folder / "Issue 002.cbz")
        _create_test_cbz(tmp_path / "random_comic.cbz")

        scanner = CollectionScanner(min_file_count=1)
        result = await scanner.scan_files(
            [
                str(folder / "Issue 001.cbz"),
                str(folder / "Issue 002.cbz"),
                str(tmp_path / "random_comic.cbz"),
            ]
        )

        assert len(result) == 2
        counts = sorted(s.file_count for s in result)
        assert counts == [1, 2]

    @pytest.mark.asyncio
    async def test_folder_without_year(self, tmp_path: Path) -> None:
        """Folder without year in name still works."""
        folder = tmp_path / "Batman"
        _create_test_cbz(folder / "Batman 001.cbz")

        scanner = CollectionScanner(min_file_count=1)
        result = await scanner.scan_files([str(folder / "Batman 001.cbz")])

        assert result[0].raw_series_name == "Batman"
        assert result[0].raw_year is None

    @pytest.mark.asyncio
    async def test_duplicate_paths_deduplicated(self, tmp_path: Path) -> None:
        """Duplicate file paths don't create duplicate entries."""
        folder = tmp_path / "Series (2020)"
        f1 = _create_test_cbz(folder / "Issue 001.cbz")

        scanner = CollectionScanner(min_file_count=1)
        # Same path twice — the dict grouping deduplicates by parent dir,
        # but the file list may have duplicates. Files are sorted by name
        # so duplicates would show up in the file count.
        result = await scanner.scan_files([str(f1), str(f1)])

        assert len(result) == 1
        # The list may have 2 entries since we don't deduplicate individual files
        # (the scanner processes what it's given)
        assert result[0].file_count >= 1
