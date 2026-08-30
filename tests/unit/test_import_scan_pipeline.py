"""Focused coverage for scan-pipeline progress behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.exc import OperationalError

from pullbox.core.collection_scanner import COMIC_EXTENSIONS, CollectionScanner, DiscoveredSeries
from pullbox.core.library_layout import ImportLayoutMode, SourceLayoutSpec
from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType
from pullbox.services.import_scan_pipeline import (
    _scan_collection_discovered_series,
    _scan_failure_message,
)
from pullbox.services.import_workflow_state import phase_progress

if TYPE_CHECKING:
    from pathlib import Path


async def test_scan_collection_discovered_series_emits_live_inventory_progress(
    db_session,
    tmp_path: Path,
) -> None:
    for idx in range(120):
        folder = tmp_path / f"Series {idx:03d}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"Issue {idx:03d}.cbz").touch()

    job = ImportJob(
        source_path=str(tmp_path),
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.SCANNING,
        min_files_per_series=1,
    )
    db_session.add(job)
    await db_session.flush()

    progress_events: list[dict[str, object]] = []

    async def resolve_import_file_extensions(_session, _formats):
        return COMIC_EXTENSIONS

    async def emit_scan_progress(
        *,
        status: ImportJobStatus,
        phase: str,
        message: str,
        progress: int,
        current_series: str | None = None,
        current_series_status=None,
    ) -> None:
        progress_events.append(
            {
                "status": status,
                "phase": phase,
                "message": message,
                "progress": progress,
                "current_series": current_series,
            }
        )

    discovered = await _scan_collection_discovered_series(
        db_session,
        job,
        scanner_cls=CollectionScanner,
        resolve_import_file_extensions=resolve_import_file_extensions,
        emit_scan_progress=emit_scan_progress,
        phase_progress=phase_progress,
    )

    inventory_events = [event for event in progress_events if event["phase"] == "inventory"]

    assert len(discovered) == 120
    assert inventory_events
    assert any("directories visited" in str(event["message"]) for event in inventory_events)
    assert max(int(event["progress"]) for event in inventory_events) > 0


async def test_scan_collection_discovered_series_skips_inventory_prepass(
    db_session,
    tmp_path: Path,
) -> None:
    job = ImportJob(
        source_path=str(tmp_path),
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.SCANNING,
        min_files_per_series=1,
    )
    db_session.add(job)
    await db_session.flush()

    async def resolve_import_file_extensions(_session, _formats):
        return COMIC_EXTENSIONS

    async def emit_scan_progress(
        *,
        status: ImportJobStatus,
        phase: str,
        message: str,
        progress: int,
        current_series: str | None = None,
        current_series_status=None,
    ) -> None:
        return None

    class ScannerDouble:
        def __init__(
            self,
            *args,
            progress_callback=None,
            file_progress_callback=None,
            inventory_progress_callback=None,
            **kwargs,
        ) -> None:
            self._progress_callback = progress_callback
            self._inventory_progress_callback = inventory_progress_callback

        async def inventory(self, _root):
            raise AssertionError("inventory() should not be called for filesystem scans")

        async def scan(self, _root):
            if self._progress_callback is not None:
                await self._progress_callback(3, 1)
            if self._inventory_progress_callback is not None:
                await self._inventory_progress_callback(1, 3)
            yield DiscoveredSeries(
                raw_series_name="Batman",
                raw_year=2016,
                raw_publisher=None,
                file_count=1,
                sample_paths=[str(tmp_path / "Batman 001.cbz")],
                source_folder=str(tmp_path),
                source_folder_relative="(root)",
                files=[],
            )

    discovered = await _scan_collection_discovered_series(
        db_session,
        job,
        scanner_cls=ScannerDouble,
        resolve_import_file_extensions=resolve_import_file_extensions,
        emit_scan_progress=emit_scan_progress,
        phase_progress=phase_progress,
    )

    assert len(discovered) == 1
    assert job.scan_total_files == 3
    assert job.scan_total_dirs == 1


async def test_scan_collection_discovered_series_passes_frozen_layout_to_scanner(
    db_session,
    tmp_path: Path,
) -> None:
    expected_layout = SourceLayoutSpec(
        mode=ImportLayoutMode.PRESET,
        preset="publisher_series",
        series_path_template="{Publisher}/{Series}",
    )
    job = ImportJob(
        source_path=str(tmp_path),
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.SCANNING,
        min_files_per_series=1,
        source_layout_snapshot=expected_layout.to_dict(),
    )
    db_session.add(job)
    await db_session.flush()
    captured_layouts: list[SourceLayoutSpec] = []

    async def resolve_import_file_extensions(_session, _formats):
        return COMIC_EXTENSIONS

    async def emit_scan_progress(**_kwargs) -> None:
        return None

    class ScannerDouble:
        def __init__(self, *args, source_layout=None, **kwargs) -> None:
            captured_layouts.append(source_layout)

        async def scan(self, _root):
            if False:
                yield None

    discovered = await _scan_collection_discovered_series(
        db_session,
        job,
        scanner_cls=ScannerDouble,
        resolve_import_file_extensions=resolve_import_file_extensions,
        emit_scan_progress=emit_scan_progress,
        phase_progress=phase_progress,
    )

    assert discovered == []
    assert captured_layouts == [expected_layout]


def test_scan_failure_message_names_sqlite_lock_contention() -> None:
    exc = OperationalError("INSERT INTO import_job_logs", {}, Exception("database is locked"))

    message = _scan_failure_message(exc)

    assert "Database was busy while saving import progress" in message
    assert "WAL mode" in message


def test_scan_failure_message_preserves_non_lock_errors() -> None:
    assert _scan_failure_message(RuntimeError("archive exploded")) == "archive exploded"
