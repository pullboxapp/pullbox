"""Unit tests for CollectionScanner."""

from __future__ import annotations

import asyncio
import gc
import os
import sqlite3
import stat
import threading
import time
import zipfile
from contextlib import closing
from itertools import pairwise
from pathlib import Path, PurePosixPath

import pytest

from pullbox.core.collection_scanner import CollectionScanner, DiscoveredSeries
from pullbox.core.exceptions import ConfigurationError
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
        assert len(updates) >= 2
        assert updates[-1] == (221, 220)
        assert all(
            later[0] >= earlier[0] and later[1] >= earlier[1]
            for earlier, later in pairwise(updates)
        )

    def test_inventory_progress_is_time_gated_instead_of_directory_bursted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.core import collection_scanner as scanner_module

        def fast_walk(self, top_down=True, on_error=None, follow_symlinks=False):
            del self, top_down, on_error, follow_symlinks
            for index in range(350):
                yield tmp_path / f"Directory {index:04d}", [], []

        monkeypatch.setattr(Path, "walk", fast_walk)
        monkeypatch.setattr(scanner_module.time, "monotonic", lambda: 100.0)
        updates: list[tuple[int, int]] = []

        result = CollectionScanner()._inventory_tree(
            tmp_path,
            lambda directory_count, file_count: updates.append((directory_count, file_count)),
        )

        assert result == (350, 0)
        assert updates == [(1, 0), (350, 0)]

    def test_spool_progress_is_time_gated_instead_of_directory_bursted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.core import collection_scanner as scanner_module

        def fast_walk(self, top_down=True, on_error=None, follow_symlinks=False):
            del self, top_down, on_error, follow_symlinks
            for index in range(350):
                yield tmp_path / f"Directory {index:04d}", [], []

        monkeypatch.setattr(Path, "walk", fast_walk)
        monkeypatch.setattr(scanner_module.time, "monotonic", lambda: 100.0)
        updates: list[tuple[int, int]] = []
        spool = scanner_module._ScanInventorySpool(tmp_path)

        try:
            result = spool.build(
                lambda directory_count, file_count: updates.append((directory_count, file_count)),
                threading.Event(),
                scanner_module.COMIC_EXTENSIONS,
            )
        finally:
            spool.close()

        assert (result.directory_count, result.file_count) == (350, 0)
        assert updates == [(1, 0), (350, 0)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("operation", ["inventory", "scan"])
    async def test_progress_delivery_uses_a_size_one_mailbox(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        operation: str,
    ) -> None:
        from pullbox.core import collection_scanner as scanner_module

        real_queue = asyncio.Queue
        created_queues: list[asyncio.Queue[object]] = []
        high_water = 0

        class TrackingQueue(real_queue):
            def __init__(self, maxsize: int = 0) -> None:
                super().__init__(maxsize=maxsize)
                created_queues.append(self)

            def put_nowait(self, item: object) -> None:
                nonlocal high_water
                super().put_nowait(item)
                high_water = max(high_water, self.qsize())

        def fast_walk(self, top_down=True, on_error=None, follow_symlinks=False):
            del self, top_down, on_error, follow_symlinks
            for index in range(350):
                yield tmp_path / f"Directory {index:04d}", [], []

        async def slow_progress(_directory_count: int, _file_count: int) -> None:
            await asyncio.sleep(0.01)

        monkeypatch.setattr(Path, "walk", fast_walk)
        monkeypatch.setattr(scanner_module.asyncio, "Queue", TrackingQueue)
        scanner = CollectionScanner(inventory_progress_callback=slow_progress)

        if operation == "inventory":
            inventory = await scanner.inventory(tmp_path)
            assert inventory == scanner_module.ScanInventory(350, 0)
        else:
            assert await _scan_all(scanner, tmp_path) == []

        assert len(created_queues) == 1
        assert created_queues[0].maxsize == 1
        assert high_water <= 1

    @pytest.mark.asyncio
    async def test_progress_finish_cancellation_at_initial_yield_drains_consumer(
        self,
    ) -> None:
        from pullbox.core import collection_scanner as scanner_module

        mailbox = scanner_module._CoalescingProgressMailbox(asyncio.get_running_loop())

        async def drain_progress() -> None:
            await mailbox.queue.get()

        drain_task = asyncio.create_task(drain_progress())
        finish_task = asyncio.create_task(mailbox.finish(drain_task))
        try:
            await asyncio.sleep(0)
            finish_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await finish_task
            assert drain_task.done()
        finally:
            drain_task.cancel()
            await asyncio.gather(drain_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_blocking_bridge_drains_worker_after_repeated_task_cancellation(
        self,
    ) -> None:
        from pullbox.core import collection_scanner as scanner_module

        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocking_work() -> int:
            started.set()
            release.wait(timeout=2)
            finished.set()
            return 7

        bridge = scanner_module._CancellationBridge(None)
        task = asyncio.create_task(bridge.run_blocking(blocking_work))
        assert await asyncio.to_thread(started.wait, 1)

        try:
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()

    @pytest.mark.asyncio
    async def test_public_inventory_without_callbacks_drains_cancelled_walk(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        received_event: list[threading.Event | None] = []

        def blocking_inventory(
            root: Path,
            progress_callback=None,
            cancellation_event: threading.Event | None = None,
        ) -> tuple[int, int]:
            del root, progress_callback
            received_event.append(cancellation_event)
            started.set()
            if cancellation_event is None:
                release.wait(timeout=2)
            else:
                cancellation_event.wait(timeout=2)
            finished.set()
            return (0, 0)

        scanner = CollectionScanner()
        monkeypatch.setattr(scanner, "_inventory_tree", blocking_inventory)
        task = asyncio.create_task(scanner.inventory(tmp_path))
        assert await asyncio.to_thread(started.wait, 1)

        try:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()

        assert received_event and received_event[0] is not None
        assert received_event[0].is_set()
        assert finished.is_set()

    @pytest.mark.asyncio
    async def test_scan_stops_filesystem_walk_when_cancellation_is_observed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class ScanCancelledError(Exception):
            pass

        cancellation_checks = 0
        directories_visited = 0

        async def cancel_scan() -> None:
            nonlocal cancellation_checks
            cancellation_checks += 1
            raise ScanCancelledError

        def slow_walk(self, top_down=True, on_error=None, follow_symlinks=False):
            nonlocal directories_visited
            del self, top_down, on_error, follow_symlinks
            for index in range(500):
                directories_visited += 1
                time.sleep(0.002)
                directory = tmp_path / f"Series {index:04d}"
                yield directory, [], ["Issue 001.cbz"]

        monkeypatch.setattr(Path, "walk", slow_walk)
        scanner = CollectionScanner(cancellation_check=cancel_scan)

        with pytest.raises(ScanCancelledError):
            _ = [candidate async for candidate in scanner.scan(tmp_path)]

        assert cancellation_checks == 1
        assert directories_visited < 100

    @pytest.mark.asyncio
    async def test_scan_polls_cancellation_inside_one_large_directory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.core import collection_scanner as scanner_module

        class ScanCancelledError(Exception):
            pass

        cancellation_checks = 0
        candidate_checks = 0

        async def cancel_on_second_poll() -> None:
            nonlocal cancellation_checks
            cancellation_checks += 1
            if cancellation_checks >= 2:
                raise ScanCancelledError

        def one_large_directory(self, top_down=True, on_error=None, follow_symlinks=False):
            del self, top_down, on_error, follow_symlinks
            yield tmp_path, [], [f"Issue {index:05d}.cbz" for index in range(4_000)]

        def slow_candidate_check(path: Path, extensions: frozenset[str]) -> bool:
            nonlocal candidate_checks
            del path, extensions
            candidate_checks += 1
            time.sleep(0.001)
            return True

        monkeypatch.setattr(Path, "walk", one_large_directory)
        monkeypatch.setattr(scanner_module, "_is_scan_candidate_file", slow_candidate_check)
        scanner = CollectionScanner(cancellation_check=cancel_on_second_poll)

        with pytest.raises(ScanCancelledError):
            await _scan_all(scanner, tmp_path)

        assert cancellation_checks == 2
        assert candidate_checks < 1_000

    @pytest.mark.asyncio
    async def test_public_inventory_polls_cancellation_inside_one_large_directory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.core import collection_scanner as scanner_module

        class ScanCancelledError(Exception):
            pass

        cancellation_checks = 0
        candidate_checks = 0

        async def cancel_on_second_poll() -> None:
            nonlocal cancellation_checks
            cancellation_checks += 1
            if cancellation_checks >= 2:
                raise ScanCancelledError

        def one_large_directory(self, top_down=True, on_error=None, follow_symlinks=False):
            del self, top_down, on_error, follow_symlinks
            yield tmp_path, [], [f"Issue {index:05d}.cbz" for index in range(4_000)]

        def slow_candidate_check(path: Path, extensions: frozenset[str]) -> bool:
            nonlocal candidate_checks
            del path, extensions
            candidate_checks += 1
            time.sleep(0.001)
            return True

        monkeypatch.setattr(Path, "walk", one_large_directory)
        monkeypatch.setattr(scanner_module, "_is_scan_candidate_file", slow_candidate_check)
        scanner = CollectionScanner(cancellation_check=cancel_on_second_poll)

        with pytest.raises(ScanCancelledError):
            await scanner.inventory(tmp_path)

        assert cancellation_checks == 2
        assert candidate_checks < 1_000


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

    @pytest.mark.asyncio
    async def test_pullbox_unknown_year_folder_supplies_trusted_comicvine_id(
        self,
        tmp_path: Path,
    ) -> None:
        _make_series_dir(
            tmp_path,
            "Asylum Ink (Unknown Year) [cv-36018]",
            files=["Asylum Ink 001 (2005).cbz", "Asylum Ink 002 (2006).cbz"],
        )

        results = await _scan_all(CollectionScanner(), tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == "Asylum Ink"
        assert results[0].raw_year is None
        assert results[0].file_count == 2
        assert results[0].folder_cv_id == 36018
        assert results[0].diagnostics["folder_identity"] == {
            "kind": "pullbox_series_folder",
            "comicvine_series_id": 36018,
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("folder_name", "expected_name", "expected_year", "expected_cv_id"),
        [
            ("Comic Cuts (1890) [cv-39459]", "Comic Cuts", 1890, 39459),
            ("Four Color (1942) [cv-927]", "Four Color", 1942, 927),
        ],
    )
    async def test_pullbox_folder_accepts_historical_years_and_short_cv_ids(
        self,
        tmp_path: Path,
        folder_name: str,
        expected_name: str,
        expected_year: int,
        expected_cv_id: int,
    ) -> None:
        _make_series_dir(
            tmp_path,
            folder_name,
            files=["Issue 1.cbz", "Issue 2.cbz"],
        )

        results = await _scan_all(CollectionScanner(), tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == expected_name
        assert results[0].raw_year == expected_year
        assert results[0].folder_cv_id == expected_cv_id
        assert results[0].diagnostics["folder_identity"] == {
            "kind": "pullbox_series_folder",
            "comicvine_series_id": expected_cv_id,
        }

    @pytest.mark.asyncio
    async def test_trusted_series_identity_keeps_different_issue_years_in_one_group(
        self,
        tmp_path: Path,
    ) -> None:
        series_dir = tmp_path / "2000 AD Free Comic Book Day (FCBD) (2011) [cv-40175]"
        series_dir.mkdir()
        _make_cbz(
            series_dir / "Issue 4 - FCBD 2014 [cv-issue-452034].cbz",
            "<ComicInfo>"
            "<Series>2000 AD Free Comic Book Day (FCBD)</Series>"
            "<Number>4</Number><Year>2014</Year><Publisher>Rebellion</Publisher>"
            "<Notes>[cv_vol_id:40175] [cv_issue_id:452034]</Notes>"
            "</ComicInfo>",
        )
        _make_cbz(
            series_dir / "Issue 13 - 2023 [cv-issue-1040279].cbz",
            "<ComicInfo>"
            "<Series>2000 AD Free Comic Book Day (FCBD)</Series>"
            "<Number>13</Number><Year>2023</Year>"
            "<Notes>[cv_vol_id:40175] [cv_issue_id:1040279]</Notes>"
            "</ComicInfo>",
        )

        results = await _scan_all(CollectionScanner(), tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == "2000 AD Free Comic Book Day (FCBD)"
        assert results[0].raw_year == 2011
        assert results[0].file_count == 2
        assert results[0].folder_cv_id == 40175
        assert results[0].comicinfo_cv_id == 40175

    @pytest.mark.asyncio
    async def test_embedded_series_identity_groups_files_without_folder_suffix(
        self,
        tmp_path: Path,
    ) -> None:
        series_dir = tmp_path / "Remede Imperial"
        series_dir.mkdir()
        for issue_number, issue_year in ((1, 2023), (2, 2024)):
            _make_cbz(
                series_dir / f"Issue {issue_number} ({issue_year}).cbz",
                "<ComicInfo>"
                "<Series>Remède Impérial - L'Étrange Médecin de la Cour</Series>"
                f"<Number>{issue_number}</Number><Year>{issue_year}</Year>"
                "<Notes>[cv_vol_id:155371]</Notes>"
                "</ComicInfo>",
            )

        results = await _scan_all(CollectionScanner(), tmp_path)

        assert len(results) == 1
        assert results[0].raw_series_name == ("Remède Impérial - L'Étrange Médecin de la Cour")
        assert results[0].raw_year is None
        assert results[0].file_count == 2
        assert results[0].comicinfo_cv_id == 155371


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
        discovered_files = [item for result in results for item in result.files]
        assert {item.source_folder_cohort_key for item in discovered_files} == {"Axis"}
        assert sorted(item.source_ordinal for item in discovered_files) == [1, 2]

    @pytest.mark.asyncio
    async def test_folder_cohort_ordinals_are_global_and_path_stable(self, tmp_path: Path) -> None:
        _make_series_dir(tmp_path, "Zulu", files=["Zulu 001.cbz"])
        _make_series_dir(tmp_path, "Alpha", files=["Alpha 001.cbz", "Alpha 002.cbz"])

        scanner = CollectionScanner()
        first = await _scan_all(scanner, tmp_path)
        second = await _scan_all(scanner, tmp_path)

        def evidence(results: list[DiscoveredSeries]) -> list[tuple[str, str | None, int | None]]:
            return sorted(
                (
                    item.file_name,
                    item.source_folder_cohort_key,
                    item.source_ordinal,
                )
                for result in results
                for item in result.files
            )

        assert (
            evidence(first)
            == evidence(second)
            == [
                ("Alpha 001.cbz", "Alpha", 1),
                ("Alpha 002.cbz", "Alpha", 2),
                ("Zulu 001.cbz", "Zulu", 3),
            ]
        )

    @pytest.mark.asyncio
    async def test_focused_scan_keeps_same_named_parent_cohorts_distinct(
        self, tmp_path: Path
    ) -> None:
        first = tmp_path / "one" / "Series" / "Series 001.cbz"
        second = tmp_path / "two" / "Series" / "Series 002.cbz"
        _touch(first)
        _touch(second)

        results = await CollectionScanner().scan_files([str(second), str(first)])
        discovered_files = [item for result in results for item in result.files]

        assert len(discovered_files) == 2
        assert len({item.source_folder_cohort_key for item in discovered_files}) == 2
        assert sorted(item.source_ordinal for item in discovered_files) == [1, 2]
        assert all(
            item.source_folder_cohort_key is not None
            and str(tmp_path) not in item.source_folder_cohort_key
            for item in discovered_files
        )

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


class TestSeriesDirectoryClassification:
    """Series-root classification must stay correct without all-pairs work."""

    def test_nested_directories_keep_only_deepest_comic_directory(self, tmp_path: Path) -> None:
        scanner = CollectionScanner()
        publisher = tmp_path / "Publisher"
        series = publisher / "Series"
        volume = series / "Volume 2"
        dir_files = {
            publisher: [publisher / "Special.cbz"],
            series: [series / "Issue 001.cbz"],
            volume: [volume / "Issue 002.cbz"],
            tmp_path / "Sibling": [tmp_path / "Sibling" / "Issue 001.cbz"],
        }

        result = scanner._identify_series_dirs(dir_files)

        assert set(result) == {volume, tmp_path / "Sibling"}

    def test_wide_tree_avoids_pairwise_descendant_checks(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.core import collection_scanner as scanner_module

        scanner = CollectionScanner()
        series_dirs = [tmp_path / "Publisher" / f"Series {index:04d}" for index in range(500)]
        dir_files = {directory: [directory / "Issue 001.cbz"] for directory in series_dirs}
        descendant_checks = 0
        original_is_descendant = scanner_module._is_descendant

        def count_descendant_check(child: Path, parent: Path) -> bool:
            nonlocal descendant_checks
            descendant_checks += 1
            return original_is_descendant(child, parent)

        monkeypatch.setattr(scanner_module, "_is_descendant", count_descendant_check)

        result = scanner._identify_series_dirs(dir_files)

        assert set(result) == set(series_dirs)
        assert descendant_checks <= len(series_dirs) * 4

    @pytest.mark.asyncio
    async def test_scan_bounds_pending_series_tasks(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.core import collection_scanner as scanner_module

        scanner = CollectionScanner()
        directory_count = 250
        for index in range(directory_count):
            _touch(tmp_path / f"Series {index:04d}" / "Issue 001.cbz")

        async def fake_build_discovered_files(*args, **kwargs):
            del args, kwargs
            await asyncio.sleep(0)
            return []

        monkeypatch.setattr(scanner, "_build_discovered_files", fake_build_discovered_files)
        monkeypatch.setattr(scanner, "_build_series_candidates", lambda **kwargs: [])

        real_create_task = asyncio.create_task
        pending_count = 0
        peak_pending_count = 0

        def track_create_task(coro):
            nonlocal pending_count, peak_pending_count
            task = real_create_task(coro)
            pending_count += 1
            peak_pending_count = max(peak_pending_count, pending_count)

            def mark_done(_task: asyncio.Task[object]) -> None:
                nonlocal pending_count
                pending_count -= 1

            task.add_done_callback(mark_done)
            return task

        monkeypatch.setattr(scanner_module.asyncio, "create_task", track_create_task)

        results = [candidate async for candidate in scanner.scan(tmp_path)]

        assert results == []
        assert peak_pending_count <= scanner_module.SERIES_SCAN_WORKERS + 1


class TestBoundedScannerState:
    """Scanner inventory and file work must stay inside explicit active windows."""

    @pytest.mark.asyncio
    async def test_folder_scan_does_not_retain_complete_path_inventory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.core import collection_scanner as scanner_module

        scanner = CollectionScanner()
        for index in range(96):
            _touch(tmp_path / f"Series {index:04d}" / "Issue 001.cbz")

        async def fake_build_discovered_files(*args, **kwargs):
            del args, kwargs
            await asyncio.sleep(0)
            return []

        monkeypatch.setattr(scanner, "_build_discovered_files", fake_build_discovered_files)
        monkeypatch.setattr(scanner, "_build_series_candidates", lambda **kwargs: [])

        assert await _scan_all(scanner, tmp_path) == []
        assert scanner.retained_inventory_path_high_water <= scanner_module.SCAN_ACTIVE_PATH_BUDGET
        assert scanner.inventory_spool_bytes > 0

    @pytest.mark.asyncio
    async def test_oversized_bucket_bounds_active_file_tasks(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.core import collection_scanner as scanner_module

        files = [tmp_path / f"Issue {index:04d}.cbz" for index in range(64)]
        for file_path in files:
            file_path.touch()

        def fake_from_path(
            self,
            archive_path,
            *,
            include_archive_comicinfo=True,
            **kwargs,
        ) -> SourceMetadata:
            del self, kwargs
            if include_archive_comicinfo:
                time.sleep(0.02)
            return SourceMetadata(original_title=Path(archive_path).name)

        scanner = CollectionScanner()
        monkeypatch.setattr(SourceMetadataExtractor, "from_path", fake_from_path)
        monkeypatch.setattr(
            scanner,
            "_should_load_archive_metadata",
            lambda *args, **kwargs: True,
        )

        discovered_files = await scanner._build_discovered_files(
            files,
            asyncio.Semaphore(scanner_module.ARCHIVE_READ_CONCURRENCY),
            root=tmp_path,
        )

        assert len(discovered_files) == len(files)
        assert scanner.active_file_task_high_water <= scanner_module.ARCHIVE_READ_CONCURRENCY

    @pytest.mark.asyncio
    async def test_oversized_buckets_are_scheduled_alone(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        oversized_count = 32
        for bucket_name in ("Alpha", "Zulu"):
            for index in range(oversized_count):
                _touch(tmp_path / bucket_name / f"{bucket_name} {index:04d}.cbz")

        active_buckets = 0
        peak_active_buckets = 0

        async def track_bucket(*args, **kwargs):
            nonlocal active_buckets, peak_active_buckets
            del args, kwargs
            active_buckets += 1
            peak_active_buckets = max(peak_active_buckets, active_buckets)
            try:
                await asyncio.sleep(0.01)
                return []
            finally:
                active_buckets -= 1

        scanner = CollectionScanner()
        monkeypatch.setattr(scanner, "_build_discovered_files", track_bucket)
        monkeypatch.setattr(scanner, "_build_series_candidates", lambda **kwargs: [])

        assert await _scan_all(scanner, tmp_path) == []
        assert peak_active_buckets == 1
        assert scanner.retained_inventory_path_high_water == oversized_count

    @pytest.mark.asyncio
    async def test_bucket_load_polls_cancellation_callback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.core import collection_scanner as scanner_module

        class ScanCancelledError(Exception):
            pass

        load_started = threading.Event()
        original_load = scanner_module._ScanInventorySpool.load_bucket_files

        async def cancel_during_load() -> None:
            if load_started.is_set():
                raise ScanCancelledError

        def slow_load(
            self,
            relative_directory: str,
            cancellation_event: threading.Event | None = None,
        ):
            load_started.set()
            if cancellation_event is None:
                time.sleep(0.75)
                return original_load(self, relative_directory)
            cancellation_event.wait(0.75)
            raise sqlite3.OperationalError("interrupted")

        monkeypatch.setattr(scanner_module._ScanInventorySpool, "load_bucket_files", slow_load)
        _touch(tmp_path / "Series" / "Series 001.cbz")
        scanner = CollectionScanner(cancellation_check=cancel_during_load)
        started_at = time.monotonic()

        with pytest.raises(ScanCancelledError):
            await _scan_all(scanner, tmp_path)

        assert time.monotonic() - started_at < 0.5

    @pytest.mark.asyncio
    async def test_simultaneous_file_failures_are_all_retrieved(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.core import collection_scanner as scanner_module

        files = [tmp_path / "Issue 001.cbz", tmp_path / "Issue 002.cbz"]
        for file_path in files:
            file_path.touch()

        arrived = 0
        release = asyncio.Event()
        unretrieved: list[dict[str, object]] = []

        def fake_from_path(self, archive_path, **kwargs) -> SourceMetadata:
            del self, kwargs
            return SourceMetadata(original_title=Path(archive_path).name)

        async def fail_together(function, path, **kwargs):
            nonlocal arrived
            del function, kwargs
            arrived += 1
            if arrived == len(files):
                release.set()
            await release.wait()
            msg = f"failed {Path(path).name}"
            raise RuntimeError(msg)

        scanner = CollectionScanner()
        monkeypatch.setattr(SourceMetadataExtractor, "from_path", fake_from_path)
        monkeypatch.setattr(scanner_module.asyncio, "to_thread", fail_together)
        monkeypatch.setattr(
            scanner,
            "_should_load_archive_metadata",
            lambda *args, **kwargs: True,
        )
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unretrieved.append(context))
        try:
            with pytest.raises(RuntimeError, match="failed Issue"):
                await scanner._build_discovered_files(
                    files,
                    asyncio.Semaphore(scanner_module.ARCHIVE_READ_CONCURRENCY),
                    root=tmp_path,
                )
            gc.collect()
            await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(previous_handler)

        assert arrived == len(files)
        assert unretrieved == []

    @pytest.mark.asyncio
    async def test_selected_file_scan_polls_during_slow_archive_task(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.core import collection_scanner as scanner_module

        class ScanCancelledError(Exception):
            pass

        file_path = tmp_path / "Issue 001.cbz"
        file_path.touch()
        archive_started = asyncio.Event()

        async def cancellation_check() -> None:
            if archive_started.is_set():
                raise ScanCancelledError

        def fake_from_path(self, archive_path, **kwargs) -> SourceMetadata:
            del self, kwargs
            return SourceMetadata(original_title=Path(archive_path).name)

        async def slow_to_thread(function, path, **kwargs):
            del function, path, kwargs
            archive_started.set()
            await asyncio.sleep(10)

        scanner = CollectionScanner(cancellation_check=cancellation_check)
        monkeypatch.setattr(SourceMetadataExtractor, "from_path", fake_from_path)
        monkeypatch.setattr(scanner_module.asyncio, "to_thread", slow_to_thread)
        monkeypatch.setattr(
            scanner,
            "_should_load_archive_metadata",
            lambda *args, **kwargs: True,
        )

        with pytest.raises(ScanCancelledError):
            await scanner.scan_files([str(file_path)])

    def test_windows_bucket_order_uses_folded_key_with_original_tie_break(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.core import collection_scanner as scanner_module

        directories = ["Zulu", "alpha", "ALPHA"]
        original_sort_key = scanner_module._directory_sort_key

        def synthetic_walk(self, top_down=True, on_error=None, follow_symlinks=False):
            del self, top_down, on_error, follow_symlinks
            yield tmp_path, directories.copy(), []
            for directory in directories:
                yield tmp_path / directory, [], ["Issue 001.cbz"]

        monkeypatch.setattr(Path, "walk", synthetic_walk)
        monkeypatch.setattr(
            scanner_module,
            "_directory_sort_key",
            lambda value: original_sort_key(value, windows=True),
        )
        spool = scanner_module._ScanInventorySpool(tmp_path)
        try:
            spool.build(None, threading.Event(), scanner_module.COMIC_EXTENSIONS)
            buckets = spool.load_bucket_page(after_order=0, limit=10)
        finally:
            spool.close()

        assert [bucket.relative_directory for bucket in buckets] == ["ALPHA", "alpha", "Zulu"]

    @pytest.mark.skipif(os.name == "nt", reason="surrogateescape paths are POSIX-only")
    def test_spool_rejects_surrogateescape_path_with_configuration_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.core import collection_scanner as scanner_module

        undecodable_name = os.fsdecode(b"Issue \xff.cbz")

        def synthetic_walk(self, top_down=True, on_error=None, follow_symlinks=False):
            del self, top_down, on_error, follow_symlinks
            yield tmp_path, ["Series"], []
            yield tmp_path / "Series", [], [undecodable_name]

        monkeypatch.setattr(Path, "walk", synthetic_walk)
        spool = scanner_module._ScanInventorySpool(tmp_path)
        try:
            with pytest.raises(ConfigurationError, match="cannot be represented safely"):
                spool.build(None, threading.Event(), scanner_module.COMIC_EXTENSIONS)
        finally:
            spool.close()

    @pytest.mark.skipif(os.name == "nt", reason="surrogateescape paths are POSIX-only")
    def test_spool_ignores_unstored_surrogateescape_directories(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.core import collection_scanner as scanner_module

        hidden_directory = os.fsdecode(b".ignored-\xff")
        empty_directory = os.fsdecode(b"empty-\xfe")
        visited: list[str] = []

        def synthetic_walk(self, top_down=True, on_error=None, follow_symlinks=False):
            del self, top_down, on_error, follow_symlinks
            dirnames = [hidden_directory, empty_directory, "Series"]
            yield tmp_path, dirnames, []
            for dirname in dirnames:
                visited.append(dirname)
                filenames = ["Series 001.cbz"] if dirname == "Series" else []
                yield tmp_path / dirname, [], filenames

        monkeypatch.setattr(Path, "walk", synthetic_walk)
        spool = scanner_module._ScanInventorySpool(tmp_path)
        try:
            result = spool.build(None, threading.Event(), scanner_module.COMIC_EXTENSIONS)
            buckets = spool.load_bucket_page(after_order=0, limit=10)
        finally:
            spool.close()

        assert hidden_directory not in visited
        assert empty_directory in visited
        assert (result.directory_count, result.file_count) == (3, 1)
        assert [bucket.relative_directory for bucket in buckets] == ["Series"]

    def test_spool_is_private_relative_parameterized_and_removed(self, tmp_path: Path) -> None:
        from pullbox.core import collection_scanner as scanner_module

        root = tmp_path / "library"
        _touch(root / "Alpha" / "Alpha 001.cbz")
        _touch(root / "Publisher's" / "Issue O'Brien 001.cbz")
        spool = scanner_module._ScanInventorySpool(root)
        directory_path = spool._directory_path
        database_path = spool._database_path

        try:
            result = spool.build(
                None,
                threading.Event(),
                scanner_module.COMIC_EXTENSIONS,
            )

            if os.name != "nt":
                assert stat.S_IMODE(directory_path.stat().st_mode) == 0o700
                assert stat.S_IMODE(database_path.stat().st_mode) == 0o600
            with closing(sqlite3.connect(database_path)) as connection:
                rows = connection.execute(
                    "SELECT relative_path, source_ordinal FROM inventory_files "
                    "ORDER BY source_ordinal"
                ).fetchall()
                directories = connection.execute(
                    "SELECT relative_directory FROM ordered_buckets"
                ).fetchall()

            assert rows == [
                ("Alpha/Alpha 001.cbz", 1),
                ("Publisher's/Issue O'Brien 001.cbz", 2),
            ]
            stored_values = [str(row[0]) for row in [*rows, *directories]]
            assert all(not PurePosixPath(value).is_absolute() for value in stored_values)
            assert all(str(root) not in value for value in stored_values)
            assert result.spool_bytes > 0
            assert result.active_path_high_water == 1
        finally:
            spool.close()

        assert not directory_path.exists()

    @pytest.mark.asyncio
    async def test_spool_cleanup_after_success(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.core import collection_scanner as scanner_module

        created_directories: list[Path] = []
        original_spool = scanner_module._ScanInventorySpool

        class TrackingSpool(original_spool):
            def __init__(self, root: Path) -> None:
                super().__init__(root)
                created_directories.append(self._directory_path)

        monkeypatch.setattr(scanner_module, "_ScanInventorySpool", TrackingSpool)
        _touch(tmp_path / "Series" / "Series 001.cbz")

        results = await _scan_all(CollectionScanner(), tmp_path)

        assert len(results) == 1
        assert created_directories
        assert all(not path.exists() for path in created_directories)

    @pytest.mark.asyncio
    async def test_spool_cleanup_after_build_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.core import collection_scanner as scanner_module

        created_directories: list[Path] = []
        original_spool = scanner_module._ScanInventorySpool

        class TrackingSpool(original_spool):
            def __init__(self, root: Path) -> None:
                super().__init__(root)
                created_directories.append(self._directory_path)

        def fail_candidate_check(*args, **kwargs):
            del args, kwargs
            msg = "synthetic spool build failure"
            raise RuntimeError(msg)

        monkeypatch.setattr(scanner_module, "_ScanInventorySpool", TrackingSpool)
        monkeypatch.setattr(scanner_module, "_is_scan_candidate_file", fail_candidate_check)
        _touch(tmp_path / "Series" / "Series 001.cbz")

        with pytest.raises(RuntimeError, match="synthetic spool build failure"):
            await _scan_all(CollectionScanner(), tmp_path)

        assert created_directories
        assert all(not path.exists() for path in created_directories)

    @pytest.mark.asyncio
    async def test_spool_cleanup_after_scan_cancellation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.core import collection_scanner as scanner_module

        created_directories: list[Path] = []
        original_spool = scanner_module._ScanInventorySpool
        processing_started = asyncio.Event()

        class TrackingSpool(original_spool):
            def __init__(self, root: Path) -> None:
                super().__init__(root)
                created_directories.append(self._directory_path)

        async def wait_forever(*args, **kwargs):
            del args, kwargs
            processing_started.set()
            await asyncio.Future()

        scanner = CollectionScanner()
        monkeypatch.setattr(scanner_module, "_ScanInventorySpool", TrackingSpool)
        monkeypatch.setattr(scanner, "_build_discovered_files", wait_forever)
        _touch(tmp_path / "Series" / "Series 001.cbz")
        generator = scanner.scan(tmp_path)
        scan_task = asyncio.create_task(anext(generator))
        await processing_started.wait()

        scan_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await scan_task
        await generator.aclose()

        assert created_directories
        assert all(not path.exists() for path in created_directories)

    @pytest.mark.asyncio
    async def test_spool_cleanup_after_generator_early_close(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.core import collection_scanner as scanner_module

        created_directories: list[Path] = []
        original_spool = scanner_module._ScanInventorySpool

        class TrackingSpool(original_spool):
            def __init__(self, root: Path) -> None:
                super().__init__(root)
                created_directories.append(self._directory_path)

        monkeypatch.setattr(scanner_module, "_ScanInventorySpool", TrackingSpool)
        _touch(tmp_path / "Alpha" / "Alpha 001.cbz")
        _touch(tmp_path / "Zulu" / "Zulu 001.cbz")
        generator = CollectionScanner().scan(tmp_path)

        first = await anext(generator)
        await generator.aclose()

        assert first.file_count == 1
        assert created_directories
        assert all(not path.exists() for path in created_directories)

    @pytest.mark.asyncio
    async def test_concurrent_bucket_inspection_preserves_scan_order(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A later fast bucket must not overtake an earlier slow bucket."""
        _touch(tmp_path / "Alpha" / "Alpha 001.cbz")
        _touch(tmp_path / "Zulu" / "Zulu 001.cbz")
        scanner = CollectionScanner()
        original_build = scanner._build_discovered_files

        async def stagger_bucket(comic_files, *args, **kwargs):
            if comic_files[0].parent.name == "Alpha":
                await asyncio.sleep(0.05)
            return await original_build(comic_files, *args, **kwargs)

        monkeypatch.setattr(scanner, "_build_discovered_files", stagger_bucket)

        results = await _scan_all(scanner, tmp_path)

        assert [result.raw_series_name for result in results] == ["Alpha", "Zulu"]

    @pytest.mark.asyncio
    async def test_scan_close_drains_pending_bucket_before_returning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _touch(tmp_path / "Alpha" / "Alpha 001.cbz")
        _touch(tmp_path / "Zulu" / "Zulu 001.cbz")
        scanner = CollectionScanner()
        original_build = scanner._build_discovered_files
        pending_started = asyncio.Event()
        pending_stopped = asyncio.Event()
        pending_tasks: list[asyncio.Task[object]] = []

        async def block_later_bucket(comic_files, *args, **kwargs):
            if comic_files[0].parent.name == "Zulu":
                task = asyncio.current_task()
                assert task is not None
                pending_tasks.append(task)
                pending_started.set()
                try:
                    await asyncio.Future()
                finally:
                    pending_stopped.set()
            return await original_build(comic_files, *args, **kwargs)

        monkeypatch.setattr(scanner, "_build_discovered_files", block_later_bucket)
        generator = scanner.scan(tmp_path)
        try:
            await anext(generator)
            await asyncio.wait_for(pending_started.wait(), timeout=1)
            await generator.aclose()
            assert pending_stopped.is_set()
        finally:
            for task in pending_tasks:
                task.cancel()
            await asyncio.gather(*pending_tasks, return_exceptions=True)
            await generator.aclose()

    @pytest.mark.asyncio
    async def test_cancellation_callback_is_suspended_while_consumer_handles_yield(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        callback_count = 0
        consumer_is_handling_yield = False

        async def cancellation_check() -> None:
            nonlocal callback_count
            assert not consumer_is_handling_yield
            callback_count += 1

        _touch(tmp_path / "Alpha" / "Alpha 001.cbz")
        _touch(tmp_path / "Zulu" / "Zulu 001.cbz")
        scanner = CollectionScanner(cancellation_check=cancellation_check)
        original_build = scanner._build_discovered_files

        async def stagger_bucket(comic_files, *args, **kwargs):
            if comic_files[0].parent.name == "Zulu":
                await asyncio.sleep(0.4)
            return await original_build(comic_files, *args, **kwargs)

        monkeypatch.setattr(scanner, "_build_discovered_files", stagger_bucket)
        generator = scanner.scan(tmp_path)

        await anext(generator)
        count_at_yield = callback_count
        consumer_is_handling_yield = True
        await asyncio.sleep(0.15)
        consumer_is_handling_yield = False

        assert callback_count == count_at_yield
        await generator.aclose()

    @pytest.mark.asyncio
    async def test_sql_finalization_surfaces_cooperative_callback_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.core import collection_scanner as scanner_module

        for index in range(300):
            _touch(tmp_path / "Series" / f"Series {index:04d}.cbz")

        created_directories: list[Path] = []
        finalization_started = threading.Event()
        handler_cleared = threading.Event()
        original_spool = scanner_module._ScanInventorySpool
        real_connect = sqlite3.connect

        class ScanCancelledError(Exception):
            pass

        async def cancel_during_finalization() -> None:
            if finalization_started.is_set():
                raise ScanCancelledError

        class TrackingSpool(original_spool):
            def __init__(self, root: Path) -> None:
                super().__init__(root)
                created_directories.append(self._directory_path)

        class BlockingProgressConnection:
            def __init__(self, database_path: Path) -> None:
                self._connection = real_connect(database_path)

            def __getattr__(self, name: str):
                return getattr(self._connection, name)

            def set_progress_handler(self, callback, instruction_count: int) -> None:
                if callback is None:
                    handler_cleared.set()
                    self._connection.set_progress_handler(None, instruction_count)
                    return

                def block_in_sql_transform() -> int:
                    finalization_started.set()
                    while callback() == 0:
                        time.sleep(0.001)
                    return 1

                self._connection.set_progress_handler(
                    block_in_sql_transform,
                    instruction_count,
                )

        monkeypatch.setattr(scanner_module, "_ScanInventorySpool", TrackingSpool)
        monkeypatch.setattr(
            scanner_module.sqlite3,
            "connect",
            lambda database_path: BlockingProgressConnection(database_path),
        )
        generator = CollectionScanner(cancellation_check=cancel_during_finalization).scan(tmp_path)
        scan_task = asyncio.create_task(anext(generator))

        with pytest.raises(ScanCancelledError):
            await asyncio.wait_for(scan_task, timeout=2)
        await generator.aclose()

        assert handler_cleared.is_set()
        assert created_directories
        assert all(not path.exists() for path in created_directories)

    @pytest.mark.asyncio
    async def test_each_archive_is_inspected_once_with_bounded_file_tasks(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        archive_reads: dict[str, int] = {}
        file_count = 24
        for index in range(file_count):
            _touch(tmp_path / "Series" / f"Series {index:04d}.cbz")

        def fake_from_path(
            self,
            archive_path,
            *,
            include_archive_comicinfo=True,
            **kwargs,
        ) -> SourceMetadata:
            del self, kwargs
            file_name = Path(archive_path).name
            if include_archive_comicinfo:
                archive_reads[file_name] = archive_reads.get(file_name, 0) + 1
            return SourceMetadata(
                original_title=file_name,
                series_name="Series",
            )

        scanner = CollectionScanner()
        monkeypatch.setattr(SourceMetadataExtractor, "from_path", fake_from_path)
        monkeypatch.setattr(
            scanner,
            "_should_load_archive_metadata",
            lambda *args, **kwargs: True,
        )

        results = await _scan_all(scanner, tmp_path)

        assert sum(result.file_count for result in results) == file_count
        assert archive_reads == {f"Series {index:04d}.cbz": 1 for index in range(file_count)}
        assert scanner.active_file_task_high_water <= 8

    @pytest.mark.asyncio
    async def test_parent_comics_stay_loose_when_descendant_is_leaf(self, tmp_path: Path) -> None:
        _touch(tmp_path / "Parent" / "Parent 001.cbz")
        _touch(tmp_path / "Parent" / "Child" / "Child 001.cbz")

        results = await _scan_all(CollectionScanner(), tmp_path)
        files_by_source = {
            result.source_folder_relative: {item.file_name for item in result.files}
            for result in results
        }

        assert files_by_source == {
            "Parent": {"Parent 001.cbz"},
            "Parent/Child": {"Child 001.cbz"},
        }
        ordinals = {
            item.file_name: item.source_ordinal for result in results for item in result.files
        }
        assert ordinals == {"Child 001.cbz": 1, "Parent 001.cbz": 2}


class TestInventorySpoolWalkHardening:
    """Verify the spooled import walk stays resilient and deterministic."""

    def test_walk_tree_sorts_files_within_directory(self, tmp_path: Path, monkeypatch) -> None:
        from pullbox.core import collection_scanner as scanner_module

        def fake_walk(self, top_down=True, on_error=None, follow_symlinks=False):
            del self, top_down, follow_symlinks
            assert on_error is not None
            yield (
                tmp_path,
                ["Series B", "Series A"],
                ["Issue 002.cbz", "Issue 001.cbz", "note.txt"],
            )

        monkeypatch.setattr(Path, "walk", fake_walk)
        spool = scanner_module._ScanInventorySpool(tmp_path)

        try:
            spool.build(None, threading.Event(), scanner_module.COMIC_EXTENSIONS)
            bucket_files = spool.load_bucket_files("")
        finally:
            spool.close()

        assert [path.name for path, _ordinal in bucket_files] == [
            "Issue 001.cbz",
            "Issue 002.cbz",
        ]

    def test_walk_tree_logs_errors_and_continues(self, tmp_path: Path, monkeypatch) -> None:
        from pullbox.core import collection_scanner as mod

        warnings: list[dict[str, str | None]] = []

        def fake_warning(event: str, **kwargs: str | None) -> None:
            warnings.append({"event": event, **kwargs})

        def fake_walk(self, top_down=True, on_error=None, follow_symlinks=False):
            del self, top_down, follow_symlinks
            assert on_error is not None
            on_error(PermissionError(13, "Permission denied", str(tmp_path / "locked")))
            yield (tmp_path, [], ["Issue 001.cbz"])

        monkeypatch.setattr(mod.logger, "warning", fake_warning)
        monkeypatch.setattr(Path, "walk", fake_walk)
        spool = mod._ScanInventorySpool(tmp_path)

        try:
            spool.build(None, threading.Event(), mod.COMIC_EXTENSIONS)
            bucket_files = spool.load_bucket_files("")
        finally:
            spool.close()

        assert [path.name for path, _ordinal in bucket_files] == ["Issue 001.cbz"]
        assert warnings == [
            {
                "event": "import_scan_walk_error",
                "path": str(tmp_path),
                "error": f"[Errno 13] Permission denied: '{tmp_path / 'locked'}'",
            }
        ]
