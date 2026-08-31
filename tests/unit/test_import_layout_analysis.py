"""Tests for bounded, read-only import layout analysis."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from pullbox.core.library_layout import ImportLayoutMode, SourceLayoutSpec
from pullbox.services.import_layout_analysis import (
    ImportLayoutAnalyzer,
    LayoutAnalysisBudget,
)

if TYPE_CHECKING:
    from pathlib import Path


def _comic(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fixture")
    return path


@pytest.mark.asyncio
async def test_analyzer_detects_series_and_publisher_series_clusters(tmp_path: Path) -> None:
    _comic(tmp_path / "Batman (2011)" / "Issue 001.cbz")
    _comic(tmp_path / "Batman (2011)" / "Issue 002.cbz")
    _comic(tmp_path / "Marvel" / "Daredevil (2019)" / "Issue 001.cbz")

    result = await ImportLayoutAnalyzer().analyze(tmp_path)

    assert result.files_considered == 3
    assert result.files_fitting == 3
    assert result.files_ambiguous == 0
    assert result.partial is False
    assert result.classification == "mixed"
    assert {cluster.proposed_series_path_template for cluster in result.clusters} == {
        "{Series}",
        "{Publisher}/{Series}",
    }
    assert sum(cluster.directory_count for cluster in result.clusters) == 2
    assert all(
        not example.relative_path.startswith("/")
        for cluster in result.clusters
        for example in cluster.examples
    )


@pytest.mark.asyncio
async def test_analyzer_applies_custom_layout_without_hiding_non_fitting_files(
    tmp_path: Path,
) -> None:
    _comic(tmp_path / "DC Comics" / "Batman" / "Issue 001 - First.cbz")
    _comic(tmp_path / "loose" / "Saga 001.cbz")
    spec = SourceLayoutSpec(
        mode=ImportLayoutMode.CUSTOM,
        series_path_template="{Publisher}/{Series}",
        issue_filename_template="Issue {Issue} - {IssueTitle}",
        fallback_to_auto=False,
    )

    result = await ImportLayoutAnalyzer().analyze(tmp_path, spec=spec)

    assert result.files_considered == 2
    assert result.files_fitting == 1
    assert result.files_ambiguous == 1
    assert any(cluster.classification == "needs_review" for cluster in result.clusters)
    assert result.can_apply_future_policy is False


@pytest.mark.asyncio
async def test_custom_auto_fallback_remains_visible_but_cannot_set_policy(
    tmp_path: Path,
) -> None:
    _comic(tmp_path / "Batman" / "Issue 001.cbz")
    spec = SourceLayoutSpec(
        mode=ImportLayoutMode.CUSTOM,
        series_path_template="{Publisher}/{Series}",
        fallback_to_auto=True,
    )

    result = await ImportLayoutAnalyzer().analyze(tmp_path, spec=spec)

    assert result.files_considered == 1
    assert result.files_fitting == 1
    assert result.files_ambiguous == 0
    assert result.clusters[0].proposed_series_path_template == "{Series}"
    assert result.can_apply_future_policy is False


@pytest.mark.asyncio
async def test_auto_analyzer_does_not_call_generic_container_a_publisher(
    tmp_path: Path,
) -> None:
    _comic(tmp_path / "Comics" / "Batman" / "Issue 001.cbz")

    result = await ImportLayoutAnalyzer().analyze(tmp_path)

    assert result.files_considered == 1
    assert result.files_fitting == 0
    assert result.files_ambiguous == 1
    assert result.classification == "needs_review"
    example = result.clusters[0].examples[0]
    assert example.publisher is None
    assert "generic_or_type_container_requires_review" in example.warnings


@pytest.mark.asyncio
async def test_analyzer_returns_truthful_partial_result_at_file_bound(tmp_path: Path) -> None:
    for number in range(3):
        _comic(tmp_path / "Batman" / f"Issue {number + 1:03d}.cbz")

    result = await ImportLayoutAnalyzer().analyze(
        tmp_path,
        budget=LayoutAnalysisBudget(max_files=1),
    )

    assert result.files_considered == 1
    assert result.partial is True
    assert "file_limit_reached" in result.warnings
    assert sum(len(cluster.examples) for cluster in result.clusters) <= 1


@pytest.mark.asyncio
async def test_analyzer_bounds_examples_without_losing_counts(tmp_path: Path) -> None:
    for number in range(5):
        _comic(tmp_path / "Batman" / f"Issue {number + 1:03d}.cbz")

    result = await ImportLayoutAnalyzer().analyze(
        tmp_path,
        budget=LayoutAnalysisBudget(max_examples_per_cluster=2),
    )

    assert result.files_considered == 5
    assert result.files_fitting == 5
    assert result.partial is False
    assert len(result.clusters) == 1
    assert len(result.clusters[0].examples) == 2


@pytest.mark.asyncio
async def test_analyzer_returns_truthful_partial_result_at_directory_bound(
    tmp_path: Path,
) -> None:
    _comic(tmp_path / "A" / "Issue 001.cbz")
    _comic(tmp_path / "B" / "Issue 001.cbz")

    result = await ImportLayoutAnalyzer().analyze(
        tmp_path,
        budget=LayoutAnalysisBudget(max_directories=1),
    )

    assert result.directories_considered == 1
    assert result.files_considered == 0
    assert result.partial is True
    assert "directory_limit_reached" in result.warnings


@pytest.mark.asyncio
async def test_analyzer_honors_deadline_without_claiming_full_coverage(tmp_path: Path) -> None:
    _comic(tmp_path / "Batman" / "Issue 001.cbz")

    result = await ImportLayoutAnalyzer().analyze(
        tmp_path,
        budget=LayoutAnalysisBudget(deadline_seconds=0),
    )

    assert result.files_considered == 0
    assert result.partial is True
    assert "deadline_reached" in result.warnings


@pytest.mark.asyncio
async def test_analyzer_cancels_before_inventory_work(tmp_path: Path) -> None:
    _comic(tmp_path / "Batman" / "Issue 001.cbz")
    cancel_event = asyncio.Event()
    cancel_event.set()

    with pytest.raises(asyncio.CancelledError):
        await ImportLayoutAnalyzer().analyze(tmp_path, cancel_event=cancel_event)


@pytest.mark.asyncio
async def test_analyzer_does_not_follow_file_symlink_outside_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = _comic(tmp_path / "outside" / "Issue 001.cbz")
    link_dir = source / "Batman"
    link_dir.mkdir()
    (link_dir / "Issue 001.cbz").symlink_to(outside)

    result = await ImportLayoutAnalyzer().analyze(source)

    assert result.files_considered == 0
    assert result.files_outside_root == 1
    assert result.files_ambiguous == 1
    assert "outside_root_skipped" in result.warnings


@pytest.mark.asyncio
async def test_analyzer_does_not_follow_symlinked_directories(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside" / "Batman"
    _comic(outside / "Issue 001.cbz")
    (source / "linked").symlink_to(outside, target_is_directory=True)

    result = await ImportLayoutAnalyzer().analyze(source)

    assert result.files_considered == 0
    assert result.partial is True
    assert "symlink_directory_skipped" in result.warnings


@pytest.mark.asyncio
async def test_analyzer_marks_walk_errors_as_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pullbox.services import import_layout_analysis

    def failing_walk(root: Path, *, topdown: bool, onerror, followlinks: bool):
        assert topdown is True
        assert followlinks is False
        onerror(PermissionError(13, "Permission denied", str(root / "private")))
        yield str(root), [], []

    monkeypatch.setattr(import_layout_analysis.os, "walk", failing_walk)

    result = await ImportLayoutAnalyzer().analyze(tmp_path)

    assert result.partial is True
    assert result.can_keep_in_place is False
    assert "unreadable_path_skipped" in result.warnings


@pytest.mark.asyncio
async def test_analyzer_is_read_only_and_does_not_probe_archives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comic = _comic(tmp_path / "Batman" / "Issue 001.cbz")
    before = (comic.read_bytes(), comic.stat().st_mtime_ns, sorted(tmp_path.rglob("*")))

    from pullbox.core import source_metadata

    def unexpected_archive_read(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("layout analysis must not read archive contents")

    monkeypatch.setattr(
        source_metadata.SourceMetadataExtractor,
        "from_path",
        unexpected_archive_read,
    )

    result = await ImportLayoutAnalyzer().analyze(tmp_path)

    after = (comic.read_bytes(), comic.stat().st_mtime_ns, sorted(tmp_path.rglob("*")))
    assert result.archive_probes == 0
    assert before == after


@pytest.mark.asyncio
async def test_analyzer_is_deterministic_and_preserves_large_issue_text(tmp_path: Path) -> None:
    _comic(tmp_path / "DC One Million" / "Issue 1000000.cbz")
    analyzer = ImportLayoutAnalyzer()

    first = await analyzer.analyze(tmp_path)
    second = await analyzer.analyze(tmp_path)

    assert first == second
    assert first.clusters[0].examples[0].issue_number == "1000000"


@pytest.mark.asyncio
async def test_empty_source_cannot_claim_a_future_layout_policy(tmp_path: Path) -> None:
    result = await ImportLayoutAnalyzer().analyze(tmp_path)

    assert result.files_considered == 0
    assert result.clusters == []
    assert result.classification == "needs_review"
    assert result.can_apply_future_policy is False
