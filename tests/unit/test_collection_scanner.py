"""Unit tests for CollectionScanner."""

from __future__ import annotations

import time
import zipfile
from itertools import pairwise
from pathlib import Path

import pytest

from pullbox.core.collection_scanner import CollectionScanner, DiscoveredSeries
from pullbox.core.library_layout import ImportLayoutMode, SourceLayoutSpec
from pullbox.core.source_metadata import MetadataSignal, SourceMetadata, SourceMetadataExtractor
from pullbox.models.issue import IssueType


def _touch(path: Path) -> None:
    """Create an empty file, ensuring parent dirs exist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def _make_series_dir(root: Path, *parts: str, files: list[str] | None = None) -> None:
    """Create a series directory with optional comic files."""
    folder = root.joinpath(*parts)
    folder.mkdir(parents=True, exist_ok=True)
    for f in files or []:
        (folder / f).touch()


async def _scan_all(scanner: CollectionScanner, root: Path) -> list[DiscoveredSeries]:
    """Collect all results from the async generator."""
    results: list[DiscoveredSeries] = []
    async for series in scanner.scan(root):
        results.append(series)
    return results


class TestFlatStructure:
    """Test 1: Flat structure — all CBZ files in single folder."""

    @pytest.mark.asyncio
    async def test_flat_multiple_series(self, tmp_path: Path) -> None:
        _touch(tmp_path / "Batman #001.cbz")
        _touch(tmp_path / "Batman #002.cbz")
        _touch(tmp_path / "Saga #001.cbz")

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) >= 1  # At least the root folder as a series
        # All files should be accounted for
        total_files = sum(r.file_count for r in results)
        assert total_files == 3


class TestInventoryProgress:
    """Inventory should emit incremental directory/file counts before completion."""

    @pytest.mark.asyncio
    async def test_inventory_emits_incremental_progress(self, tmp_path: Path) -> None:
        for idx in range(220):
            folder = tmp_path / f"Series {idx:03d}"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"Issue {idx:03d}.cbz").touch()

        updates: list[tuple[int, int]] = []

        async def capture_progress(directory_count: int, file_count: int) -> None:
            updates.append((directory_count, file_count))

        scanner = CollectionScanner(inventory_progress_callback=capture_progress)

        inventory = await scanner.inventory(tmp_path)

        assert inventory.directory_count == 221
        assert inventory.file_count == 220
        assert len(updates) >= 3
        assert updates[-1] == (221, 220)
        assert all(
            later[0] >= earlier[0] and later[1] >= earlier[1]
            for earlier, later in pairwise(updates)
        )


class TestTwoLevelWithYear:
    """Test 2: Two-level structure with year in folder name."""

    @pytest.mark.asyncio
    async def test_two_level_year_extraction(self, tmp_path: Path) -> None:
        _make_series_dir(
            tmp_path,
            "Batman (2016)",
            files=[
                "Batman 001.cbz",
                "Batman 002.cbz",
                "Batman 003.cbz",
                "Batman 004.cbz",
                "Batman 005.cbz",
            ],
        )
        _make_series_dir(
            tmp_path,
            "Saga (2012)",
            files=[
                "Saga 001.cbz",
                "Saga 002.cbz",
                "Saga 003.cbz",
            ],
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 2
        by_name = {r.raw_series_name: r for r in results}
        assert "Batman" in by_name
        assert "Saga" in by_name
        assert by_name["Batman"].raw_year == 2016
        assert by_name["Saga"].raw_year == 2012
        assert by_name["Batman"].file_count == 5
        assert by_name["Saga"].file_count == 3


class TestThreeLevelWithPublisher:
    """Test 3: Three-level structure with publisher folder."""

    @pytest.mark.asyncio
    async def test_publisher_inference(self, tmp_path: Path) -> None:
        _make_series_dir(
            tmp_path,
            "DC Comics",
            "Batman (2016)",
            files=[
                "Batman 001.cbz",
                "Batman 002.cbz",
            ],
        )
        _make_series_dir(
            tmp_path,
            "Image Comics",
            "Saga (2012)",
            files=[
                "Saga 001.cbz",
            ],
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 2
        by_name = {r.raw_series_name: r for r in results}
        assert by_name["Batman"].raw_publisher == "DC Comics"
        assert by_name["Saga"].raw_publisher == "Image Comics"


class TestSelectedSourceLayout:
    """Explicit source layouts should drive grouping without hiding fallback files."""

    @pytest.mark.asyncio
    async def test_non_fitting_file_requires_review_without_auto_fallback(
        self,
        tmp_path: Path,
    ) -> None:
        _touch(tmp_path / "Batman (2011)" / "Batman 001.cbz")
        scanner = CollectionScanner(
            source_layout=SourceLayoutSpec(
                mode=ImportLayoutMode.CUSTOM,
                series_path_template="{Publisher}/{Series}",
                fallback_to_auto=False,
            )
        )

        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == "Batman"
        assert results[0].files[0].metadata_diagnostics["source_layout"] == {
            "fit": False,
            "fallback_used": False,
            "review_required": True,
            "review_reason": "selected_layout_no_match",
            "relative_path": "Batman (2011)/Batman 001.cbz",
        }

    @pytest.mark.asyncio
    async def test_custom_layout_uses_higher_series_segment_and_exact_issue_text(
        self,
        tmp_path: Path,
    ) -> None:
        _touch(
            tmp_path
            / "DC Comics"
            / "Absolute Batman (2024)"
            / "Issues"
            / "Issue 1000000 - The Zoo.cbz"
        )
        scanner = CollectionScanner(
            source_layout=SourceLayoutSpec(
                mode=ImportLayoutMode.CUSTOM,
                series_path_template="{Publisher}/{Series} ({Year})/{Type}",
                issue_filename_template="Issue {Issue} - {IssueTitle}",
                fallback_to_auto=True,
            )
        )

        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        candidate = results[0]
        assert candidate.raw_series_name == "Absolute Batman"
        assert candidate.raw_year == 2024
        assert candidate.raw_publisher == "DC Comics"
        discovered_file = candidate.files[0]
        assert discovered_file.parsed_series == "Absolute Batman"
        assert discovered_file.issue_number_raw == "1000000"
        assert discovered_file.metadata_signals["series_name"] == "source_layout"
        assert discovered_file.metadata_signals["issue_number"] == "source_layout"
        assert discovered_file.metadata_diagnostics["source_layout"] == {
            "fit": True,
            "fallback_used": False,
            "relative_path": (
                "DC Comics/Absolute Batman (2024)/Issues/Issue 1000000 - The Zoo.cbz"
            ),
            "issue_title": "The Zoo",
        }

    @pytest.mark.asyncio
    async def test_non_fitting_file_uses_auto_only_when_fallback_is_enabled(
        self,
        tmp_path: Path,
    ) -> None:
        _touch(tmp_path / "Batman (2011)" / "Batman 001.cbz")
        scanner = CollectionScanner(
            source_layout=SourceLayoutSpec(
                mode=ImportLayoutMode.CUSTOM,
                series_path_template="{Publisher}/{Series}",
                fallback_to_auto=True,
            )
        )

        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == "Batman"
        assert results[0].files[0].metadata_diagnostics["source_layout"] == {
            "fit": False,
            "fallback_used": True,
            "relative_path": "Batman (2011)/Batman 001.cbz",
        }

    @pytest.mark.asyncio
    async def test_exact_embedded_identity_wins_over_conflicting_selected_layout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        comic_path = tmp_path / "DC Comics" / "Layout Batman" / "Batman 001.cbz"
        _touch(comic_path)

        def exact_metadata(*_args, **_kwargs) -> SourceMetadata:
            return SourceMetadata(
                original_title=comic_path.name,
                source_path=str(comic_path),
                series_name="Canonical Batman",
                issue_number=1.0,
                year=2011,
                publisher="DC Comics",
                signals={
                    "series_name": MetadataSignal.COMICINFO,
                    "issue_number": MetadataSignal.COMICINFO,
                    "year": MetadataSignal.COMICINFO,
                    "publisher": MetadataSignal.COMICINFO,
                },
                diagnostics={"has_comicinfo": True},
            )

        monkeypatch.setattr(SourceMetadataExtractor, "from_path", exact_metadata)
        scanner = CollectionScanner(
            source_layout=SourceLayoutSpec(
                mode=ImportLayoutMode.PRESET,
                preset="publisher_series",
            )
        )

        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == "Canonical Batman"
        discovered_file = results[0].files[0]
        assert discovered_file.metadata_signals["series_name"] == "comicinfo"
        assert discovered_file.metadata_diagnostics["source_layout_conflicts"] == {
            "series_name": {
                "selected": "Layout Batman",
                "preserved_signal": "comicinfo",
            }
        }


class TestDeepNesting:
    """Test 4: Deep nesting (4+ levels)."""

    @pytest.mark.asyncio
    async def test_deep_hierarchy(self, tmp_path: Path) -> None:
        _make_series_dir(
            tmp_path,
            "ext",
            "backups",
            "old",
            "DC",
            "Batman (2016)",
            files=[
                "Batman 001.cbz",
                "Batman 002.cbz",
            ],
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == "Batman"
        assert results[0].raw_year == 2016

    @pytest.mark.asyncio
    async def test_pullbox_folder_suffix_supplies_trusted_comicvine_id(
        self,
        tmp_path: Path,
    ) -> None:
        _make_series_dir(
            tmp_path,
            "Batman (2016) 97508",
            files=["Batman 001 (2016).cbz", "Batman 002 (2016).cbz"],
        )

        results = await _scan_all(CollectionScanner(), tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == "Batman"
        assert results[0].raw_year == 2016
        assert results[0].folder_cv_id == 97508
        assert results[0].diagnostics["folder_identity"] == {
            "kind": "pullbox_series_folder",
            "comicvine_series_id": 97508,
        }

    @pytest.mark.asyncio
    async def test_pullbox_collision_folder_suffix_supplies_trusted_comicvine_id(
        self,
        tmp_path: Path,
    ) -> None:
        _make_series_dir(
            tmp_path,
            "Batman (2024) [cv-162083]",
            files=["Batman 001 (2024).cbz", "Batman 002 (2024).cbz"],
        )

        results = await _scan_all(CollectionScanner(), tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == "Batman"
        assert results[0].raw_year == 2024
        assert results[0].folder_cv_id == 162083
        assert results[0].diagnostics["folder_identity"] == {
            "kind": "pullbox_series_folder",
            "comicvine_series_id": 162083,
        }


class TestMixedFormats:
    """Test 5: Mixed formats in same series folder."""

    @pytest.mark.asyncio
    async def test_cbz_cbr_pdf_counted(self, tmp_path: Path) -> None:
        _make_series_dir(
            tmp_path,
            "Batman (2016)",
            files=[
                "Batman 001.cbz",
                "Batman 002.cbr",
                "Batman Annual 001.pdf",
            ],
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        by_name = {result.raw_series_name: result for result in results}
        assert set(by_name) == {"Batman", "Batman Annual"}
        assert by_name["Batman"].file_count == 2
        assert by_name["Batman Annual"].file_count == 1
        assert sum(len(result.sample_paths) for result in results) == 3


class TestNonComicFilesIgnored:
    """Test 6: Non-comic files are ignored."""

    @pytest.mark.asyncio
    async def test_non_comic_excluded(self, tmp_path: Path) -> None:
        _make_series_dir(
            tmp_path,
            "Batman (2016)",
            files=[
                "Batman 001.cbz",
                "Thumbs.db",
                ".DS_Store",
                "cover.jpg",
            ],
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].file_count == 1


class TestSeriesNameNormalization:
    """Test 7: Series name normalized — strip punctuation, normalize spaces."""

    @pytest.mark.asyncio
    async def test_name_and_year_parsing(self, tmp_path: Path) -> None:
        _make_series_dir(
            tmp_path,
            "The Amazing Spider-Man  (2015)",
            files=[
                "The Amazing Spider-Man 001.cbz",
            ],
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == "The Amazing Spider-Man"
        assert results[0].raw_year == 2015

    @pytest.mark.asyncio
    async def test_folder_year_survives_trailing_release_tags(self, tmp_path: Path) -> None:
        _make_series_dir(
            tmp_path,
            "About Betty's Boob (2018) (digital) (Mr Norrell-Empire)",
            files=[
                "comic.cbz",
            ],
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == "About Betty's Boob"
        assert results[0].raw_year == 2018

    @pytest.mark.asyncio
    async def test_folder_year_parsed_from_intermediate_suffix_folder(self, tmp_path: Path) -> None:
        _make_series_dir(
            tmp_path,
            "Thanos - The Infinity Revelation (2014) (Digital) (Kileko-Empire).#16",
            files=[
                "comic.cbz",
            ],
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == "Thanos - The Infinity Revelation"
        assert results[0].raw_year == 2014


class TestVolumeIndicator:
    """Test 8: Volume indicator preserved in name."""

    @pytest.mark.asyncio
    async def test_volume_in_folder_name(self, tmp_path: Path) -> None:
        _make_series_dir(
            tmp_path,
            "Batman Vol. 2 (2011)",
            files=[
                "Batman 001.cbz",
            ],
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        # Volume indicator should be preserved in the name
        assert "Batman" in results[0].raw_series_name
        assert results[0].raw_year == 2011


class TestEmptyFoldersSkipped:
    """Test 9: Empty folders are skipped."""

    @pytest.mark.asyncio
    async def test_empty_folder(self, tmp_path: Path) -> None:
        (tmp_path / "EmptySeries (2020)").mkdir()

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 0


class TestMinimumFileThreshold:
    """Test 10: Single-file folders still included."""

    @pytest.mark.asyncio
    async def test_single_file_series(self, tmp_path: Path) -> None:
        _make_series_dir(
            tmp_path,
            "Batman Noel (2011)",
            files=[
                "Batman Noel.cbz",
            ],
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].file_count == 1


class TestMylar3StyleNaming:
    """Test 11: Mylar3-style naming convention."""

    @pytest.mark.asyncio
    async def test_mylar3_naming(self, tmp_path: Path) -> None:
        _make_series_dir(
            tmp_path,
            "Batman (2016)",
            files=[
                "Batman v2016 001 (Digital).cbz",
            ],
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == "Batman"
        assert results[0].raw_year == 2016


class TestDotSeparatedFilenames:
    """Test 12: Dot-separated filenames in folder."""

    @pytest.mark.asyncio
    async def test_dot_separated_file(self, tmp_path: Path) -> None:
        _make_series_dir(
            tmp_path,
            "Saga (2012)",
            files=[
                "Saga.012.2013.digital.Pyrate-DCP.cbz",
            ],
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].file_count == 1


class TestMetadataGrouping:
    """Scanner groups release-noise variants without collapsing real mixed titles."""

    @pytest.mark.asyncio
    async def test_release_noise_variants_stay_in_same_series_bucket(self, tmp_path: Path) -> None:
        _make_series_dir(
            tmp_path,
            "Batman (2016)",
            files=[
                "Batman #001 (2016).cbz",
                "Batman 001 (scan).cbz",
                "Batman 001 dup.cbz",
            ],
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == "Batman"
        assert results[0].file_count == 3

    @pytest.mark.asyncio
    async def test_distinct_titles_in_same_folder_still_split(self, tmp_path: Path) -> None:
        _make_series_dir(
            tmp_path,
            "staging",
            files=[
                "Absolute Martian Manhunter 002.cbz",
                "Chicken Devil 004.cbz",
                "Abattoir 004.cbz",
            ],
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 3
        assert {result.raw_series_name for result in results} == {
            "Absolute Martian Manhunter",
            "Chicken Devil",
            "Abattoir",
        }

    @pytest.mark.asyncio
    async def test_issue_titles_after_numbers_use_the_series_folder_identity(
        self, tmp_path: Path
    ) -> None:
        _make_series_dir(
            tmp_path,
            "Absolute Batman 2024",
            files=[
                "Issue 14 Abomination, Conclusion.cbz",
                "Issue 15 The Joker.cbz",
            ],
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == "Absolute Batman"
        assert results[0].raw_year == 2024
        assert results[0].file_count == 2
        assert {file.parsed_series for file in results[0].files} == {"Absolute Batman"}
        assert {file.parsed_issue_number for file in results[0].files} == {14.0, 15.0}

    @pytest.mark.asyncio
    async def test_issue_titles_before_issue_markers_use_the_series_folder_identity(
        self, tmp_path: Path
    ) -> None:
        _make_series_dir(
            tmp_path,
            "Batman (2011)",
            files=[
                "Batman The Court of Owls, Part One Issue 001.cbz",
                "Batman The City of Owls, Part Two Issue 002.cbz",
            ],
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == "Batman"
        assert results[0].raw_year == 2011
        assert results[0].file_count == 2
        assert {file.parsed_series for file in results[0].files} == {"Batman"}
        assert {file.parsed_issue_number for file in results[0].files} == {1.0, 2.0}

    @pytest.mark.asyncio
    async def test_ordered_mixed_series_folder_is_not_collapsed_to_the_folder_name(
        self, tmp_path: Path
    ) -> None:
        _make_series_dir(
            tmp_path,
            "Axis",
            files=[
                "001 - Avengers 001.cbz",
                "002 - X-Men 001.cbz",
            ],
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 2
        assert all(result.raw_series_name != "Axis" for result in results)
        assert all(result.diagnostics["mixed_bucket"] is True for result in results)

    @pytest.mark.asyncio
    async def test_issue_title_files_in_generic_staging_folder_stay_split(
        self, tmp_path: Path
    ) -> None:
        _make_series_dir(
            tmp_path,
            "staging",
            files=[
                "Issue 14 Abomination, Conclusion.cbz",
                "Issue 15 The Joker.cbz",
            ],
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 2
        assert all(result.raw_series_name != "staging" for result in results)
        assert all(result.diagnostics["mixed_bucket"] is True for result in results)

    @pytest.mark.asyncio
    async def test_real_series_named_issue_keeps_its_folder_identity(self, tmp_path: Path) -> None:
        _make_series_dir(
            tmp_path,
            "Issue (2021)",
            files=["Issue 001.cbz", "Issue 002.cbz"],
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == "Issue"
        assert results[0].raw_year == 2021
        assert results[0].file_count == 2

    @pytest.mark.asyncio
    async def test_issue_title_files_at_scan_root_do_not_assume_root_is_a_series(
        self, tmp_path: Path
    ) -> None:
        _touch(tmp_path / "Issue 14 Abomination, Conclusion.cbz")
        _touch(tmp_path / "Issue 15 The Joker.cbz")

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 2
        assert all(result.raw_series_name != tmp_path.name for result in results)
        assert all(result.diagnostics["mixed_bucket"] is True for result in results)

    @pytest.mark.asyncio
    async def test_issue_title_and_agreeing_comicinfo_series_share_one_folder_bucket(
        self, tmp_path: Path
    ) -> None:
        series_dir = tmp_path / "Batman (2011)"
        series_dir.mkdir()
        _make_cbz(
            series_dir / "cover.cbz",
            "<ComicInfo><Series>Batman</Series><Number>1</Number></ComicInfo>",
        )
        _touch(series_dir / "Batman The Court of Owls, Part Two Issue 002.cbz")

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == "Batman"
        assert results[0].file_count == 2
        assert results[0].diagnostics["mixed_bucket"] is False

    @pytest.mark.asyncio
    async def test_conflicting_comicinfo_series_stays_separate_from_folder_identity(
        self, tmp_path: Path
    ) -> None:
        series_dir = tmp_path / "Batman (2011)"
        series_dir.mkdir()
        _make_cbz(
            series_dir / "cover.cbz",
            "<ComicInfo><Series>Superman</Series><Number>1</Number></ComicInfo>",
        )
        _touch(series_dir / "Batman The Court of Owls, Part Two Issue 002.cbz")

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 2
        assert {result.raw_series_name for result in results} == {"Batman", "Superman"}
        assert all(result.diagnostics["mixed_bucket"] is True for result in results)

    @pytest.mark.asyncio
    async def test_issue_title_grouping_preserves_publisher_hierarchy(self, tmp_path: Path) -> None:
        _make_series_dir(
            tmp_path,
            "DC Comics",
            "Absolute Batman 2024",
            files=[
                "Issue 14 Abomination, Conclusion.cbz",
                "Issue 15 The Joker.cbz",
            ],
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == "Absolute Batman"
        assert results[0].raw_publisher == "DC Comics"

    @pytest.mark.asyncio
    async def test_folder_sidecar_preserves_series_type_and_cv_id(self, tmp_path: Path) -> None:
        folder = tmp_path / "Absolute Martian Manhunter [TPB]"
        folder.mkdir()
        (folder / "series.json").write_text(
            '{"comicid": 168590, "booktype": "TPB", "status": "Ended", "total_issues": 1}'
        )
        (folder / "Absolute Martian Manhunter Vol. 1.cbz").touch()

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        series = results[0]
        assert series.comicinfo_cv_id == 168590
        assert series.diagnostics["source_issue_type"] == IssueType.TPB.value
        assert series.diagnostics["series_status"] == "Ended"
        assert series.diagnostics["issue_count_hint"] == 1

    @pytest.mark.asyncio
    async def test_root_loose_collection_files_are_not_bucketed_under_import_root(
        self, tmp_path: Path
    ) -> None:
        _make_series_dir(tmp_path, "Batman (2016)", files=["Batman 001.cbz"])
        _touch(tmp_path / "Fearscape.Vol.01.2019.pdf")
        _touch(tmp_path / "The.Cape.Omnibus.2025.HYBRID.COMIC.pdf")
        _touch(tmp_path / "Payment Confirmation.pdf")

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        by_name = {result.raw_series_name: result for result in results}
        assert "Batman" in by_name
        assert "Fearscape" in by_name
        assert "The Cape" in by_name
        assert tmp_path.name not in by_name
        assert "Payment Confirmation" not in by_name
        assert by_name["Fearscape"].file_count == 1
        assert by_name["The Cape"].diagnostics["source_issue_type"] == IssueType.OMNIBUS.value

    @pytest.mark.asyncio
    async def test_strong_filename_folder_scan_defers_archive_reads(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _make_series_dir(
            tmp_path,
            "Batman (2016)",
            files=[
                "Batman 001.cbz",
                "Batman 002.cbz",
                "Batman 003.cbz",
            ],
        )

        archive_read_count = 0
        original_reader = SourceMetadataExtractor._read_archive_evidence

        def counting_reader(path: Path, **kwargs: object):
            nonlocal archive_read_count
            archive_read_count += 1
            return original_reader(path, **kwargs)

        monkeypatch.setattr(
            SourceMetadataExtractor,
            "_read_archive_evidence",
            staticmethod(counting_reader),
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert archive_read_count == 0

    @pytest.mark.asyncio
    async def test_sidecar_series_ids_do_not_force_archive_reads(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        folder = tmp_path / "Absolute Martian Manhunter [TPB]"
        folder.mkdir()
        (folder / "series.json").write_text(
            '{"comicid": 168590, "booktype": "TPB", "status": "Ended", "total_issues": 1}'
        )
        (folder / "Absolute Martian Manhunter Vol. 1.cbz").touch()

        archive_read_count = 0
        original_reader = SourceMetadataExtractor._read_archive_evidence

        def counting_reader(path: Path, **kwargs: object):
            nonlocal archive_read_count
            archive_read_count += 1
            return original_reader(path, **kwargs)

        monkeypatch.setattr(
            SourceMetadataExtractor,
            "_read_archive_evidence",
            staticmethod(counting_reader),
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].comicinfo_cv_id == 168590
        assert archive_read_count == 0

    @pytest.mark.asyncio
    async def test_ambiguous_files_still_load_archive_metadata(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        folder = tmp_path / "Batman (2016)"
        folder.mkdir()
        archive_path = folder / "Batman.cbz"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "ComicInfo.xml",
                (
                    "<?xml version='1.0'?>"
                    "<ComicInfo>"
                    "<Series>Batman</Series>"
                    "<Number>1</Number>"
                    "<Volume>2016</Volume>"
                    "</ComicInfo>"
                ),
            )

        archive_read_count = 0
        original_reader = SourceMetadataExtractor._read_archive_evidence

        def counting_reader(path: Path, **kwargs: object):
            nonlocal archive_read_count
            archive_read_count += 1
            return original_reader(path, **kwargs)

        monkeypatch.setattr(
            SourceMetadataExtractor,
            "_read_archive_evidence",
            staticmethod(counting_reader),
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].files[0].parsed_issue_number == 1.0
        assert archive_read_count >= 1


class TestLargeScanPerformance:
    """Test 13: Large scan performance."""

    @pytest.mark.asyncio
    async def test_large_scan_timing(self, tmp_path: Path) -> None:
        # Create 100 series x 20 files = 2000 files (reduced from 500x20 for CI speed)
        for i in range(100):
            series_dir = tmp_path / f"Series {i:03d} ({2000 + i % 26})"
            series_dir.mkdir()
            for j in range(20):
                (series_dir / f"Issue {j:03d}.cbz").touch()

        scanner = CollectionScanner()
        start = time.monotonic()
        results = await _scan_all(scanner, tmp_path)
        elapsed = time.monotonic() - start

        assert len(results) == 100
        assert elapsed < 30  # generous timeout


class TestAsyncGenerator:
    """Test 14: Scanner is an async generator yielding progressively."""

    @pytest.mark.asyncio
    async def test_yields_progressively(self, tmp_path: Path) -> None:
        _make_series_dir(tmp_path, "Batman (2016)", files=["001.cbz"])
        _make_series_dir(tmp_path, "Saga (2012)", files=["001.cbz"])

        scanner = CollectionScanner()
        count = 0
        async for _series in scanner.scan(tmp_path):
            count += 1
        assert count == 2


class TestSamplePathsCapped:
    """Sample paths should be capped at max_sample_paths."""

    @pytest.mark.asyncio
    async def test_sample_paths_limited(self, tmp_path: Path) -> None:
        _make_series_dir(
            tmp_path, "Batman (2016)", files=[f"Batman {i:03d}.cbz" for i in range(20)]
        )

        scanner = CollectionScanner(max_sample_paths=5)
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].file_count == 20
        assert len(results[0].sample_paths) == 5


class TestSourceFolderRelative:
    """Source folder relative path is correctly computed."""

    @pytest.mark.asyncio
    async def test_relative_path(self, tmp_path: Path) -> None:
        _make_series_dir(
            tmp_path,
            "DC Comics",
            "Batman (2016)",
            files=[
                "Batman 001.cbz",
            ],
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].source_folder_relative == "DC Comics/Batman (2016)"


class TestProgressCallback:
    """Progress callback is called during scan."""

    @pytest.mark.asyncio
    async def test_progress_called(self, tmp_path: Path) -> None:
        _make_series_dir(tmp_path, "Batman (2016)", files=["001.cbz", "002.cbz"])

        calls: list[tuple[int, int]] = []

        async def on_progress(files: int, dirs: int) -> None:
            calls.append((files, dirs))

        scanner = CollectionScanner(progress_callback=on_progress)
        await _scan_all(scanner, tmp_path)

        assert len(calls) > 0


def _make_cbz(path: Path, xml_content: str | None = None) -> None:
    """Create a CBZ file with optional ComicInfo.xml."""
    with zipfile.ZipFile(path, "w") as zf:
        if xml_content is not None:
            zf.writestr("ComicInfo.xml", xml_content)
        zf.writestr("page001.jpg", b"fake image")


class TestCustomExtensions:
    """Test custom extensions filter on CollectionScanner."""

    @pytest.mark.asyncio
    async def test_custom_extensions_filter(self, tmp_path: Path) -> None:
        """Only files matching custom extensions are scanned."""
        _make_series_dir(
            tmp_path,
            "Batman (2016)",
            files=["Batman 001.cbz", "Batman 002.cbr", "Batman 003.pdf"],
        )

        scanner = CollectionScanner(extensions=frozenset({".cbz"}))
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].file_count == 1  # only .cbz counted

    @pytest.mark.asyncio
    async def test_none_extensions_uses_defaults(self, tmp_path: Path) -> None:
        """None extensions falls back to COMIC_EXTENSIONS."""
        _make_series_dir(
            tmp_path,
            "Batman (2016)",
            files=["Batman 001.cbz", "Batman 002.cbr", "Batman 003.pdf"],
        )

        scanner = CollectionScanner(extensions=None)
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].file_count == 3  # all default formats

    @pytest.mark.asyncio
    async def test_custom_extensions_exclude_all(self, tmp_path: Path) -> None:
        """If custom extensions don't match any files, no series found."""
        _make_series_dir(
            tmp_path,
            "Batman (2016)",
            files=["Batman 001.cbz", "Batman 002.cbr"],
        )

        scanner = CollectionScanner(extensions=frozenset({".mobi"}))
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 0


class TestComicInfoEnrichment:
    """Scanner enriches series with ComicInfo.xml metadata."""

    @pytest.mark.asyncio
    async def test_comicinfo_overrides_folder_name(self, tmp_path: Path) -> None:
        series_dir = tmp_path / "Batman (2016)"
        series_dir.mkdir()
        _make_cbz(
            series_dir / "Batman 001.cbz",
            '<?xml version="1.0"?>'
            "<ComicInfo>"
            "<Series>Batman</Series>"
            "<Volume>2016</Volume>"
            "<Publisher>DC Comics</Publisher>"
            "<Notes>[cvid:97508]</Notes>"
            "</ComicInfo>",
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == "Batman"
        assert results[0].raw_year == 2016
        assert results[0].raw_publisher == "DC Comics"
        assert results[0].comicinfo_cv_id == 97508
        assert results[0].comicinfo_source is not None

    @pytest.mark.asyncio
    async def test_comicinfo_cv_id_none_when_no_comicinfo(self, tmp_path: Path) -> None:
        _make_series_dir(tmp_path, "Saga (2012)", files=["Saga 001.cbz"])

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].comicinfo_cv_id is None
        assert results[0].comicinfo_source is None

    @pytest.mark.asyncio
    async def test_comicinfo_overrides_publisher_from_hierarchy(self, tmp_path: Path) -> None:
        series_dir = tmp_path / "Wrong Publisher" / "Batman (2016)"
        series_dir.mkdir(parents=True)
        _make_cbz(
            series_dir / "Batman 001.cbz",
            "<ComicInfo><Publisher>DC Comics</Publisher></ComicInfo>",
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].raw_publisher == "DC Comics"

    @pytest.mark.asyncio
    async def test_comicinfo_series_name_overrides_folder(self, tmp_path: Path) -> None:
        series_dir = tmp_path / "bman (2016)"
        series_dir.mkdir()
        _make_cbz(
            series_dir / "001.cbz",
            "<ComicInfo><Series>Batman</Series></ComicInfo>",
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == "Batman"


class TestEdgeCases:
    """Edge case tests for CollectionScanner."""

    @pytest.mark.asyncio
    async def test_mixed_case_extensions(self, tmp_path: Path) -> None:
        """Files with uppercase/mixed-case extensions are still detected."""
        _make_series_dir(
            tmp_path,
            "Batman (2016)",
            files=["Batman 001.CBZ", "Batman 002.Cbr", "Batman 003.PDF"],
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].file_count == 3

    @pytest.mark.asyncio
    async def test_symlink_directory(self, tmp_path: Path) -> None:
        """A symlinked directory with comic files should not crash the scanner.

        Path.walk() does not follow symlinks by default, so the symlinked
        directory is skipped — but the scan must complete without error.
        """
        real_dir = tmp_path / "real" / "Batman (2016)"
        real_dir.mkdir(parents=True)
        (real_dir / "Batman 001.cbz").touch()
        (real_dir / "Batman 002.cbz").touch()

        link_target = tmp_path / "scan_root"
        link_target.mkdir()
        symlink = link_target / "Batman (2016)"
        symlink.symlink_to(real_dir)

        scanner = CollectionScanner()
        # Should not raise — graceful handling of symlinks
        results = await _scan_all(scanner, link_target)
        # Symlinked dirs are not followed by Path.walk(), so 0 results expected
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_hidden_directories_skipped(self, tmp_path: Path) -> None:
        """Directories starting with '.' should be skipped."""
        # Visible series — should be found
        _make_series_dir(tmp_path, "Batman (2016)", files=["Batman 001.cbz"])
        # Hidden directory — should be skipped
        _make_series_dir(tmp_path, ".hidden_series", files=["Secret 001.cbz"])

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == "Batman"

    @pytest.mark.asyncio
    async def test_macosx_directory_skipped(self, tmp_path: Path) -> None:
        """__MACOSX folder is in IGNORE_DIRS and should be skipped."""
        _make_series_dir(tmp_path, "Batman (2016)", files=["Batman 001.cbz"])
        _make_series_dir(tmp_path, "__MACOSX", files=["._Batman 001.cbz"])

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == "Batman"

    @pytest.mark.asyncio
    async def test_appledouble_sidecar_files_skipped(self, tmp_path: Path) -> None:
        """macOS AppleDouble resource-fork files should not enter import review."""
        _make_series_dir(
            tmp_path,
            "Batman (2016)",
            files=["Batman 001.cbz", "._Batman 001.cbz"],
        )

        scanner = CollectionScanner()
        inventory = await scanner.inventory(tmp_path)
        results = await _scan_all(scanner, tmp_path)

        assert inventory.file_count == 1
        assert len(results) == 1
        assert results[0].raw_series_name == "Batman"
        assert [file.file_name for file in results[0].files] == ["Batman 001.cbz"]

    @pytest.mark.asyncio
    async def test_recycle_directory_skipped(self, tmp_path: Path) -> None:
        """#recycle (Synology recycle folder) is in IGNORE_DIRS."""
        _make_series_dir(tmp_path, "Batman (2016)", files=["Batman 001.cbz"])
        _make_series_dir(tmp_path, "#recycle", files=["Deleted 001.cbz"])

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == "Batman"

    @pytest.mark.asyncio
    async def test_folder_without_year(self, tmp_path: Path) -> None:
        """A folder named 'Batman' (no year) yields raw_year=None."""
        _make_series_dir(tmp_path, "Batman", files=["Batman 001.cbz"])

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == "Batman"
        assert results[0].raw_year is None

    @pytest.mark.asyncio
    async def test_folder_with_trailing_punctuation(self, tmp_path: Path) -> None:
        """Trailing punctuation (,;:!) is stripped from folder names.

        Note: the year regex requires the pattern to end with ')' so
        'Batman!!' (no year) and 'Saga,' are the testable cases.
        """
        _make_series_dir(tmp_path, "Batman!!", files=["Batman 001.cbz"])
        _make_series_dir(tmp_path, "Saga,", files=["Saga 001.cbz"])

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        by_name = {r.raw_series_name: r for r in results}
        assert "Batman" in by_name
        assert "Saga" in by_name

    @pytest.mark.asyncio
    async def test_min_file_count_threshold(self, tmp_path: Path) -> None:
        """With min_file_count=3, a folder with only 2 files is excluded."""
        _make_series_dir(
            tmp_path,
            "Batman (2016)",
            files=["Batman 001.cbz", "Batman 002.cbz"],
        )
        _make_series_dir(
            tmp_path,
            "Saga (2012)",
            files=["Saga 001.cbz", "Saga 002.cbz", "Saga 003.cbz"],
        )

        scanner = CollectionScanner(min_file_count=3)
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == "Saga"

    @pytest.mark.asyncio
    async def test_non_directory_root_raises_valueerror(self, tmp_path: Path) -> None:
        """Passing a file path (not a directory) to scan() raises ValueError."""
        file_path = tmp_path / "not_a_dir.cbz"
        file_path.touch()

        scanner = CollectionScanner()
        with pytest.raises(ValueError, match="not a directory"):
            await _scan_all(scanner, file_path)

    @pytest.mark.asyncio
    async def test_generic_container_name_skipped_for_publisher(self, tmp_path: Path) -> None:
        """Parent folder named 'Comics' or 'downloads' is not inferred as publisher."""
        _make_series_dir(
            tmp_path,
            "Comics",
            "Batman (2016)",
            files=["Batman 001.cbz"],
        )
        _make_series_dir(
            tmp_path,
            "downloads",
            "Saga (2012)",
            files=["Saga 001.cbz"],
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        for r in results:
            assert r.raw_publisher is None, (
                f"Expected no publisher for '{r.raw_series_name}' under generic container, "
                f"got '{r.raw_publisher}'"
            )

    @pytest.mark.asyncio
    async def test_year_only_parent_folder_not_publisher(self, tmp_path: Path) -> None:
        """Parent folder named '2016' should not be inferred as publisher."""
        _make_series_dir(
            tmp_path,
            "2016",
            "Batman (2016)",
            files=["Batman 001.cbz"],
        )

        scanner = CollectionScanner()
        results = await _scan_all(scanner, tmp_path)

        assert len(results) == 1
        assert results[0].raw_publisher is None


class TestWalkTreeHardening:
    """Verify the import scanner walk stays resilient and deterministic."""

    def test_walk_tree_sorts_files_within_directory(self, tmp_path: Path, monkeypatch) -> None:
        scanner = CollectionScanner()

        def fake_walk(self, top_down=True, on_error=None, follow_symlinks=False):
            del self, top_down, follow_symlinks
            assert on_error is not None
            yield (
                tmp_path,
                ["Series B", "Series A"],
                ["Issue 002.cbz", "Issue 001.cbz", "note.txt"],
            )

        monkeypatch.setattr(Path, "walk", fake_walk)

        dir_files = scanner._walk_tree(tmp_path)

        assert list(dir_files.keys()) == [tmp_path]
        assert [path.name for path in dir_files[tmp_path]] == ["Issue 001.cbz", "Issue 002.cbz"]

    def test_walk_tree_logs_errors_and_continues(self, tmp_path: Path, monkeypatch) -> None:
        from pullbox.core import collection_scanner as mod

        warnings: list[dict[str, str | None]] = []
        scanner = CollectionScanner()

        def fake_warning(event: str, **kwargs: str | None) -> None:
            warnings.append({"event": event, **kwargs})

        def fake_walk(self, top_down=True, on_error=None, follow_symlinks=False):
            del self, top_down, follow_symlinks
            assert on_error is not None
            on_error(PermissionError(13, "Permission denied", str(tmp_path / "locked")))
            yield (tmp_path, [], ["Issue 001.cbz"])

        monkeypatch.setattr(mod.logger, "warning", fake_warning)
        monkeypatch.setattr(Path, "walk", fake_walk)

        dir_files = scanner._walk_tree(tmp_path)

        assert [path.name for path in dir_files[tmp_path]] == ["Issue 001.cbz"]
        assert warnings == [
            {
                "event": "import_scan_walk_error",
                "path": str(tmp_path),
                "error": f"[Errno 13] Permission denied: '{tmp_path / 'locked'}'",
            }
        ]
