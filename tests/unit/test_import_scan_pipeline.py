"""Focused coverage for scan-pipeline progress behavior."""

from __future__ import annotations

import gc
import json
import weakref
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from pullbox.core.collection_scanner import (
    COMIC_EXTENSIONS,
    CollectionScanner,
    DiscoveredFile,
    DiscoveredSeries,
)
from pullbox.core.exceptions import JobCancelledError, ValidationError
from pullbox.core.library_layout import ImportLayoutMode, SourceLayoutSpec
from pullbox.core.mylar3_reader import (
    Mylar3ArcSettingsSnapshot,
    Mylar3CollectionSnapshot,
    Mylar3ImportMetadataSnapshot,
    Mylar3StoryArcSnapshot,
)
from pullbox.models.import_job import (
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSourceType,
)
from pullbox.models.issue import IssueType
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.services.import_scan_materialization import materialize_discovered_scan_results
from pullbox.services.import_scan_pipeline import (
    _load_mylar3_discovered_series,
    _scan_collection_discovered_series,
    _scan_failure_message,
    run_import_scan_pipeline,
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

    assert discovered == 120
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

    assert discovered == 1
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

    assert discovered == 0
    assert captured_layouts == [expected_layout]


async def test_filesystem_scan_passes_job_cancellation_into_inventory_walk(
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
    cancellation_calls: list[int] = []

    async def resolve_import_file_extensions(_session, _formats):
        return COMIC_EXTENSIONS

    async def emit_scan_progress(**_kwargs) -> None:
        return None

    async def raise_if_cancelled(_session, job_id: int) -> None:
        cancellation_calls.append(job_id)

    class ScannerDouble:
        def __init__(self, *args, cancellation_check=None, **kwargs) -> None:
            del args, kwargs
            self._cancellation_check = cancellation_check

        async def scan(self, _root):
            assert self._cancellation_check is not None
            await self._cancellation_check()
            if False:
                yield None

    discovered = await _scan_collection_discovered_series(
        db_session,
        job,
        scanner_cls=ScannerDouble,
        resolve_import_file_extensions=resolve_import_file_extensions,
        emit_scan_progress=emit_scan_progress,
        phase_progress=phase_progress,
        raise_if_cancelled=raise_if_cancelled,
    )

    assert discovered == 0
    assert cancellation_calls == [job.id]


async def test_filesystem_scan_returns_count_without_retaining_materialized_series(
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
    discovered_refs: list[weakref.ReferenceType[DiscoveredSeries]] = []

    async def resolve_import_file_extensions(_session, _formats):
        return COMIC_EXTENSIONS

    async def emit_scan_progress(**_kwargs) -> None:
        return None

    class ScannerDouble:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        async def scan(self, _root):
            for index in range(75):
                discovered = DiscoveredSeries(
                    raw_series_name=f"Series {index:03d}",
                    raw_year=2026,
                    raw_publisher="Example",
                    file_count=0,
                    sample_paths=[],
                    source_folder=str(tmp_path / f"Series {index:03d}"),
                    source_folder_relative=f"Series {index:03d}",
                    files=[],
                )
                discovered_refs.append(weakref.ref(discovered))
                yield discovered

    series_count = await _scan_collection_discovered_series(
        db_session,
        job,
        scanner_cls=ScannerDouble,
        resolve_import_file_extensions=resolve_import_file_extensions,
        emit_scan_progress=emit_scan_progress,
        phase_progress=phase_progress,
    )
    gc.collect()

    assert series_count == 75
    assert all(reference() is None for reference in discovered_refs)


async def test_load_mylar3_discovered_series_passes_frozen_layout_to_reader(
    db_session,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "mylar.db"
    db_path.touch()
    expected_layout = SourceLayoutSpec(
        mode=ImportLayoutMode.PRESET,
        preset="publisher_series",
        series_path_template="{Publisher}/{Series}",
        fallback_to_auto=False,
    )
    job = ImportJob(
        source_path=str(db_path),
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.SCANNING,
        source_layout_snapshot=expected_layout.to_dict(),
        mylar3_path_map_confirmed=True,
    )
    db_session.add(job)
    await db_session.flush()
    captured_layouts: list[SourceLayoutSpec] = []

    class ReaderDouble:
        def __init__(self, *, db_path, path_map, source_layout) -> None:
            captured_layouts.append(source_layout)

        async def read_series(self) -> list[DiscoveredSeries]:
            return []

    async def log_event(*_args, **_kwargs) -> None:
        return None

    discovered_count = await _load_mylar3_discovered_series(
        db_session,
        job,
        job_id=job.id,
        mylar3_reader_cls=ReaderDouble,
        auto_detect_mylar3_path_map=lambda _path: None,
        log_event=log_event,
    )

    assert discovered_count == 0
    assert captured_layouts == [expected_layout]


async def test_load_mylar3_rejects_unconfirmed_legacy_path_map_instead_of_detecting(
    db_session,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "mylar.db"
    db_path.touch()
    host_root = tmp_path / "host-comics"
    host_root.mkdir()
    detected = {"/comics": str(host_root)}
    job = ImportJob(
        source_path=str(db_path),
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.SCANNING,
    )
    db_session.add(job)
    await db_session.flush()
    job_id = job.id
    captured_maps: list[dict[str, str] | None] = []
    detector_calls: list[Path] = []

    class ReaderDouble:
        def __init__(self, *, db_path, path_map, source_layout) -> None:
            captured_maps.append(path_map)

        async def read_series(self) -> list[DiscoveredSeries]:
            return []

    async def log_event(*_args, **_kwargs) -> None:
        return None

    def detect(path: Path) -> dict[str, str]:
        detector_calls.append(path)
        return detected

    with pytest.raises(ValidationError, match="Step 1"):
        await _load_mylar3_discovered_series(
            db_session,
            job,
            job_id=job_id,
            mylar3_reader_cls=ReaderDouble,
            auto_detect_mylar3_path_map=detect,
            log_event=log_event,
        )

    assert detector_calls == []
    assert captured_maps == []
    db_session.expire_all()
    persisted = await db_session.get(ImportJob, job_id)
    assert persisted is not None
    assert persisted.mylar3_path_map == {}


async def test_load_mylar3_respects_confirmed_empty_identity_map(
    db_session,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "mylar.db"
    db_path.touch()
    job = ImportJob(
        source_path=str(db_path),
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.SCANNING,
        mylar3_path_map={},
        mylar3_path_map_confirmed=True,
    )
    db_session.add(job)
    await db_session.flush()
    detected = {"/stored/comics": str(tmp_path / "unexpected")}
    captured_maps: list[dict[str, str] | None] = []
    detector_calls: list[Path] = []

    class ReaderDouble:
        def __init__(self, *, db_path, path_map, source_layout) -> None:
            captured_maps.append(path_map)

        async def read_series(self) -> list[DiscoveredSeries]:
            return []

    def detect(path: Path) -> dict[str, str]:
        detector_calls.append(path)
        return detected

    async def log_event(*_args, **_kwargs) -> None:
        return None

    await _load_mylar3_discovered_series(
        db_session,
        job,
        job_id=job.id,
        mylar3_reader_cls=ReaderDouble,
        auto_detect_mylar3_path_map=detect,
        log_event=log_event,
    )

    assert detector_calls == []
    assert captured_maps == [None]
    assert job.mylar3_path_map == {}


async def test_load_mylar3_discovered_series_logs_path_resolution_counts(
    db_session,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "mylar.db"
    db_path.touch()
    job = ImportJob(
        source_path=str(db_path),
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.SCANNING,
        mylar3_path_map_confirmed=True,
    )
    db_session.add(job)
    await db_session.flush()

    def discovered(name: str, status: str, *, mapping_applied: bool) -> DiscoveredSeries:
        return DiscoveredSeries(
            raw_series_name=name,
            raw_year=2024,
            raw_publisher=None,
            file_count=0,
            sample_paths=[],
            source_folder="",
            source_folder_relative="",
            files=[],
            has_files=False,
            diagnostics={
                "mylar3_path": {
                    "status": status,
                    "mapping_applied": mapping_applied,
                }
            },
        )

    class ReaderDouble:
        def __init__(self, **_kwargs) -> None:
            return None

        async def read_series(self) -> list[DiscoveredSeries]:
            return [
                discovered("Mapped", "mapped", mapping_applied=True),
                discovered("Local", "local", mapping_applied=False),
                discovered("Unsafe", "invalid", mapping_applied=True),
            ]

    events: list[tuple[str, dict[str, object]]] = []

    async def log_event(_session, _job_id, _level, event, **kwargs) -> None:
        events.append((event, kwargs))

    await _load_mylar3_discovered_series(
        db_session,
        job,
        job_id=job.id,
        mylar3_reader_cls=ReaderDouble,
        auto_detect_mylar3_path_map=lambda _path: None,
        log_event=log_event,
    )

    summary = next(details for event, details in events if event == "mylar3_path_resolution")
    assert summary["path_status_counts"] == {
        "invalid": 1,
        "local": 1,
        "mapped": 1,
    }
    assert summary["mapping_applied_series"] == 2
    assert summary["incompatible_series"] == 1


async def test_load_mylar3_stages_arcs_from_one_complete_snapshot(
    db_session,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "mylar.db"
    db_path.touch()
    job = ImportJob(
        source_path=str(db_path),
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.SCANNING,
        mylar3_path_map_confirmed=True,
    )
    db_session.add(job)
    await db_session.flush()

    discovered = DiscoveredSeries(
        raw_series_name="Alpha",
        raw_year=2024,
        raw_publisher="Example",
        file_count=0,
        sample_paths=[],
        source_folder="",
        source_folder_relative="",
        files=[],
        has_files=False,
    )
    snapshot = Mylar3CollectionSnapshot(
        series=(discovered,),
        story_arcs=(
            Mylar3StoryArcSnapshot(
                story_arc_id="arc-1",
                cv_arc_id="4045-1",
                name="Synthetic Crossover",
                entries=(),
            ),
        ),
        storyarcs_present=True,
        readlist_present=True,
        readlist_count=3,
        arc_settings=Mylar3ArcSettingsSnapshot(
            present=False,
            parse_warnings=(),
            values=(),
        ),
    )
    reads: list[str] = []

    class ReaderDouble:
        def __init__(self, **_kwargs) -> None:
            return None

        async def read_collection(self) -> Mylar3CollectionSnapshot:
            reads.append("collection")
            return snapshot

        async def read_snapshot(self) -> Mylar3CollectionSnapshot:
            raise AssertionError("The Mylar source must not be read twice")

        async def read_series(self) -> list[DiscoveredSeries]:
            raise AssertionError("The Mylar source must not be read twice")

    events: list[tuple[str, dict[str, object]]] = []
    cancellation_checks = 0

    async def log_event(_session, _job_id, _level, event, **kwargs) -> None:
        events.append((event, kwargs))

    async def raise_if_cancelled(_session, _job_id) -> None:
        nonlocal cancellation_checks
        cancellation_checks += 1

    discovered_count = await _load_mylar3_discovered_series(
        db_session,
        job,
        job_id=job.id,
        mylar3_reader_cls=ReaderDouble,
        auto_detect_mylar3_path_map=lambda _path: None,
        log_event=log_event,
        raise_if_cancelled=raise_if_cancelled,
    )

    assert discovered_count == 1
    assert reads == ["collection"]
    assert cancellation_checks >= 2
    assert await db_session.scalar(select(func.count()).select_from(ImportedStoryArc)) == 1
    summary = next(
        details for event, details in events if event == "import_story_arc_staging_completed"
    )
    assert summary == {
        "message": "Story-arc evidence staged for review",
        "source_type": "mylar3",
        "arcs_staged": 1,
        "entries_staged": 0,
        "needs_review": 0,
        "cohorts_examined": 0,
        "cohorts_skipped": 0,
        "readlist_present": True,
        "readlist_count": 3,
    }
    assert str(tmp_path) not in json.dumps(summary)


async def test_load_mylar3_streams_bounded_pages_to_persistence(
    db_session,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "mylar.db"
    db_path.touch()
    job = ImportJob(
        source_path=str(db_path),
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.SCANNING,
        mylar3_path_map_confirmed=True,
    )
    db_session.add(job)
    await db_session.flush()
    page_size = 13
    total_series = 103
    discovered_refs: list[weakref.ReferenceType[DiscoveredSeries]] = []
    yielded_series_page_sizes: list[int] = []
    yielded_arc_page_sizes: list[int] = []

    class ReaderDouble:
        def __init__(self, **_kwargs) -> None:
            return None

        async def read_import_metadata(self) -> Mylar3ImportMetadataSnapshot:
            return Mylar3ImportMetadataSnapshot(
                storyarcs_present=True,
                readlist_present=True,
                readlist_count=4,
                arc_settings=Mylar3ArcSettingsSnapshot(
                    present=False,
                    parse_warnings=(),
                    values=(),
                ),
            )

        async def iter_import_story_arc_pages(self):
            for start in range(0, 5, 2):
                page = tuple(
                    Mylar3StoryArcSnapshot(
                        story_arc_id=f"arc-{index}",
                        cv_arc_id=None,
                        name=f"Arc {index}",
                        entries=(),
                    )
                    for index in range(start, min(start + 2, 5))
                )
                yielded_arc_page_sizes.append(len(page))
                yield page

        async def iter_import_series_pages(self):
            for start in range(0, total_series, page_size):
                page_items: list[DiscoveredSeries] = []
                for index in range(start, min(start + page_size, total_series)):
                    item = DiscoveredSeries(
                        raw_series_name=f"Series {index:03d}",
                        raw_year=2026,
                        raw_publisher="Fixture Comics",
                        file_count=0,
                        sample_paths=[],
                        source_folder=f"/unmounted/Series {index:03d}",
                        source_folder_relative=f"Series {index:03d}",
                        files=[],
                        has_files=False,
                        mylar3_cv_id=800_000 + index,
                        diagnostics={
                            "mylar3_path": {
                                "status": "missing",
                                "mapping_applied": False,
                            }
                        },
                    )
                    discovered_refs.append(weakref.ref(item))
                    page_items.append(item)
                yielded_series_page_sizes.append(len(page_items))
                yield tuple(page_items)

    validated_batch_sizes: list[int] = []
    materialized_batch_sizes: list[int] = []
    cancellation_checks = 0
    events: list[tuple[str, dict[str, object]]] = []

    async def validate_batch(_session, batch, **_kwargs) -> None:
        validated_batch_sizes.append(len(batch))

    async def materialize_batch(session, import_job, batch):
        materialized_batch_sizes.append(len(batch))
        return await materialize_discovered_scan_results(session, import_job, batch)

    async def raise_if_cancelled(_session, _job_id) -> None:
        nonlocal cancellation_checks
        cancellation_checks += 1

    async def log_event(_session, _job_id, _level, event, **kwargs) -> None:
        events.append((event, kwargs))

    series_count = await _load_mylar3_discovered_series(
        db_session,
        job,
        job_id=job.id,
        mylar3_reader_cls=ReaderDouble,
        auto_detect_mylar3_path_map=lambda _path: None,
        log_event=log_event,
        validate_discovered_files_safety=validate_batch,
        materialize_discovered_scan_results=materialize_batch,
        raise_if_cancelled=raise_if_cancelled,
    )
    gc.collect()

    assert series_count == total_series
    assert max(yielded_series_page_sizes) <= page_size
    assert validated_batch_sizes == yielded_series_page_sizes
    assert materialized_batch_sizes == yielded_series_page_sizes
    assert max(yielded_arc_page_sizes) <= 2
    assert await db_session.scalar(select(func.count()).select_from(ImportedSeries)) == total_series
    assert await db_session.scalar(select(func.count()).select_from(ImportedStoryArc)) == 5
    staged_arcs = list(
        (
            await db_session.execute(
                select(ImportedStoryArc).order_by(ImportedStoryArc.source_ordinal)
            )
        )
        .scalars()
        .all()
    )
    assert [arc.source_ordinal for arc in staged_arcs] == [1, 2, 3, 4, 5]
    assert cancellation_checks >= len(yielded_series_page_sizes) + len(yielded_arc_page_sizes)
    assert all(reference() is None for reference in discovered_refs)
    batch_metrics = [details for event, details in events if event == "import_mylar_batch_scanned"]
    assert len(batch_metrics) == len(yielded_series_page_sizes)
    assert all(details["inspection_duration_ms"] >= 0 for details in batch_metrics)
    assert all(details["persistence_duration_ms"] >= 0 for details in batch_metrics)
    summaries = [
        details for event, details in events if event == "import_story_arc_staging_completed"
    ]
    assert summaries == [
        {
            "message": "Story-arc evidence staged for review",
            "source_type": "mylar3",
            "arcs_staged": 5,
            "entries_staged": 0,
            "needs_review": 0,
            "cohorts_examined": 0,
            "cohorts_skipped": 0,
            "readlist_present": True,
            "readlist_count": 4,
        }
    ]


async def test_mylar_scan_checkpoints_progress_between_source_pages(db_session, tmp_path):
    job = ImportJob(
        source_path=str(tmp_path / "mylar.db"),
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.SCANNING,
        mylar3_path_map_confirmed=True,
        progress_snapshot={"progress": 0},
    )
    db_session.add(job)
    await db_session.commit()
    checkpoints = []

    class ReaderDouble:
        def __init__(self, **_kwargs):
            pass

        async def read_import_metadata(self):
            return Mylar3ImportMetadataSnapshot(
                storyarcs_present=False,
                readlist_present=False,
                readlist_count=0,
                arc_settings=Mylar3ArcSettingsSnapshot(False, (), ()),
            )

        async def iter_import_story_arc_pages(self):
            if False:
                yield ()

        async def iter_import_series_pages(self):
            for index in range(3):
                if index:
                    checkpoints.append(dict(job.progress_snapshot))
                    assert job.progress_snapshot.get("progress", 0) > 0
                    assert job.progress_snapshot.get("series_found") == index
                    assert job.scan_completed_at is None
                yield (
                    DiscoveredSeries(
                        raw_series_name=f"Series {index}",
                        raw_year=2026,
                        raw_publisher=None,
                        file_count=0,
                        sample_paths=[],
                        source_folder=f"/comics/{index}",
                        source_folder_relative=str(index),
                        files=[],
                    ),
                )

    async def no_op(*_args, **_kwargs):
        pass

    await _load_mylar3_discovered_series(
        db_session,
        job,
        job_id=job.id,
        mylar3_reader_cls=ReaderDouble,
        auto_detect_mylar3_path_map=lambda _path: None,
        log_event=no_op,
        materialize_discovered_scan_results=materialize_discovered_scan_results,
    )
    assert len(checkpoints) == 2
    assert checkpoints[1]["progress_revision"] > checkpoints[0]["progress_revision"]
    assert job.scan_completed_at is not None


async def test_mylar_scan_publishes_real_safety_batch_and_overall_progress(db_session, tmp_path):
    from pullbox.core.mylar3_reader import Mylar3Reader
    from pullbox.services.import_scan_helpers import validate_discovered_files_safety
    from pullbox.ui.import_progress_snapshot import build_import_progress_snapshot
    from scripts.mylar3_import_fixture import create_minimal_cbz, create_mylar3_db

    db = tmp_path / "mylar.db"
    rows = []
    for i in range(2):
        folder = tmp_path / f"Series{i}"
        for issue in range(3):
            create_minimal_cbz(folder / f"Series{i} {issue}.cbz")
        rows.append(
            {"ComicID": f"CV-{100 + i}", "ComicName": f"Series{i}", "ComicLocation": str(folder)}
        )
    create_mylar3_db(db, series=rows)

    class SmallPageReader(Mylar3Reader):
        async def iter_import_series_pages(self):
            async for page in super().iter_import_series_pages(page_size=1):
                yield page

    job = ImportJob(
        source_path=str(db),
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.SCANNING,
        mylar3_path_map_confirmed=True,
    )
    db_session.add(job)
    await db_session.commit()
    events = []

    async def progress(event):
        events.append(event)
        hydrated = build_import_progress_snapshot(
            job, review_summary={}, recent_logs=[], progress_revision=job.progress_revision
        )
        assert hydrated["progress"] == event.progress
        assert hydrated["series_found"] == event.series_found
        assert job.scan_completed_at is None

    async def no_op(*_args, **_kwargs):
        pass

    await _load_mylar3_discovered_series(
        db_session,
        job,
        job_id=job.id,
        mylar3_reader_cls=SmallPageReader,
        auto_detect_mylar3_path_map=lambda _path: None,
        log_event=no_op,
        validate_discovered_files_safety=validate_discovered_files_safety,
        materialize_discovered_scan_results=materialize_discovered_scan_results,
        progress_callback=progress,
    )
    assert any(event.progress == 22 and event.series_found == 1 for event in events)
    assert events[-1].progress == 35
    assert events[-1].series_found == 2
    assert events[-1].scan_total_files == 6
    assert any("3/3" in event.message for event in events)
    assert not any("1/1" in event.message for event in events)
    assert [event.progress for event in events] == sorted(event.progress for event in events)
    assert len({event.progress_revision for event in events}) == len(events)


async def test_load_mylar3_cancellation_stops_before_next_page_persistence(
    db_session,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "mylar.db"
    db_path.touch()
    job = ImportJob(
        source_path=str(db_path),
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.SCANNING,
        mylar3_path_map_confirmed=True,
    )
    db_session.add(job)
    await db_session.flush()

    class ReaderDouble:
        def __init__(self, **_kwargs) -> None:
            return None

        async def read_import_metadata(self) -> Mylar3ImportMetadataSnapshot:
            return Mylar3ImportMetadataSnapshot(
                storyarcs_present=False,
                readlist_present=False,
                readlist_count=0,
                arc_settings=Mylar3ArcSettingsSnapshot(
                    present=False,
                    parse_warnings=(),
                    values=(),
                ),
            )

        async def iter_import_story_arc_pages(self):
            if False:
                yield ()

        async def iter_import_series_pages(self):
            for page_index in range(10):
                yield (
                    DiscoveredSeries(
                        raw_series_name=f"Series {page_index}",
                        raw_year=2026,
                        raw_publisher=None,
                        file_count=0,
                        sample_paths=[],
                        source_folder="",
                        source_folder_relative="",
                        files=[],
                        has_files=False,
                    ),
                )

    materialized_pages = 0

    async def validate_batch(_session, _batch, **_kwargs) -> None:
        return None

    async def materialize_batch(session, import_job, batch):
        nonlocal materialized_pages
        materialized_pages += 1
        return await materialize_discovered_scan_results(session, import_job, batch)

    async def raise_if_cancelled(_session, _job_id) -> None:
        if materialized_pages >= 2:
            raise JobCancelledError

    async def log_event(*_args, **_kwargs) -> None:
        return None

    with pytest.raises(JobCancelledError):
        await _load_mylar3_discovered_series(
            db_session,
            job,
            job_id=job.id,
            mylar3_reader_cls=ReaderDouble,
            auto_detect_mylar3_path_map=lambda _path: None,
            log_event=log_event,
            validate_discovered_files_safety=validate_batch,
            materialize_discovered_scan_results=materialize_batch,
            raise_if_cancelled=raise_if_cancelled,
        )

    assert materialized_pages == 2
    assert await db_session.scalar(select(func.count()).select_from(ImportedSeries)) == 2


async def test_filesystem_stages_one_complete_cohort_after_all_incremental_batches(
    db_session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    job = ImportJob(
        source_path=str(tmp_path),
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.PENDING,
        min_files_per_series=1,
    )
    db_session.add(job)
    await db_session.commit()

    materialized_batch_sizes: list[int] = []

    async def materialize_batch(session, import_job, discovered_list):
        materialized_batch_sizes.append(len(discovered_list))
        return await materialize_discovered_scan_results(session, import_job, discovered_list)

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
            self._file_progress_callback = file_progress_callback

        async def scan(self, _root):
            if self._progress_callback is not None:
                await self._progress_callback(26, 1)
            # Synthetic folder identity, not a credential.
            cohort_key = "Publisher/Synthetic Crossover"  # gitleaks:allow
            for ordinal in range(1, 27):
                if self._file_progress_callback is not None:
                    await self._file_progress_callback(1)
                file_name = f"{ordinal:03d} - Series {ordinal:02d} 001.cbz"
                yield DiscoveredSeries(
                    raw_series_name=f"Series {ordinal:02d}",
                    raw_year=2024,
                    raw_publisher="Example",
                    file_count=1,
                    sample_paths=[str(tmp_path / file_name)],
                    source_folder=str(tmp_path),
                    source_folder_relative="Synthetic Crossover",
                    files=[
                        DiscoveredFile(
                            file_path=str(tmp_path / file_name),
                            file_name=file_name,
                            file_size=123,
                            file_format="cbz",
                            parsed_series=f"Series {ordinal:02d}",
                            parsed_issue_number=1.0,
                            parsed_year=2024,
                            parsed_publisher="Example",
                            has_comicinfo=True,
                            comicvine_issue_id=None,
                            issue_number_raw="1",
                            issue_type=IssueType.ISSUE,
                            metadata_diagnostics={
                                "archive_member_evidence": {
                                    "member_index_scanned": True,
                                    "comicinfo": {
                                        "series": f"Series {ordinal:02d}",
                                        "number": "1",
                                        "story_arc": "Synthetic Crossover",
                                        "story_arc_number": str(ordinal),
                                    },
                                }
                            },
                            source_folder_cohort_key=cohort_key,
                            source_ordinal=ordinal,
                        )
                    ],
                )

    events: list[tuple[str, dict[str, object]]] = []
    milestones: list[str] = []
    cancellation_checks = 0
    progress_events = []

    async def capture_progress(_job, event, **_kwargs):
        progress_events.append(event)

    monkeypatch.setattr(
        "pullbox.services.import_scan_pipeline.emit_live_progress", capture_progress
    )

    async def log_event(_session, _job_id, _level, event, **kwargs) -> None:
        events.append((event, kwargs))
        if event == "import_story_arc_resolution_completed":
            milestones.append("story_arc_resolution")

    async def raise_if_cancelled(_session, _job_id) -> None:
        nonlocal cancellation_checks
        cancellation_checks += 1

    async def no_op(*_args, **_kwargs) -> None:
        return None

    async def resolve_extensions(_session, _formats):
        return COMIC_EXTENSIONS

    async def run_file_matching(*_args, **_kwargs) -> None:
        milestones.append("file_matching")

    async def finish_series_matching(*_args, **_kwargs) -> None:
        job.progress_snapshot = {"progress": 49, "phase": "matching"}

    async def capture_persisted_progress(_session, _job, event, _callback):
        if event.phase == "file_matching":
            assert event.progress == 49
            assert event.estimated_seconds_remaining is None

    await run_import_scan_pipeline(
        db_session,
        job.id,
        scanner_cls=ScannerDouble,
        mylar3_reader_cls=object,
        auto_detect_mylar3_path_map=lambda _path: None,
        reset_scan_artifacts=no_op,
        resolve_import_file_extensions=resolve_extensions,
        validate_discovered_files_safety=no_op,
        materialize_discovered_scan_results=materialize_batch,
        deduplicate_series=no_op,
        run_matching=finish_series_matching,
        consolidate_logical_series_groups=no_op,
        run_file_matching=run_file_matching,
        raise_if_cancelled=raise_if_cancelled,
        log_event=log_event,
        emit_progress=capture_persisted_progress,
        phase_progress=phase_progress,
        estimate_remaining_seconds=lambda _started_at, _progress: None,
        job_stats=lambda _job: {},
        maybe_slow_phase_delay=no_op,
        progress_callback=no_op,
    )

    assert materialized_batch_sizes == [25, 1]
    assert job.series_found == 26
    assert await db_session.scalar(select(func.count()).select_from(ImportedStoryArc)) == 1
    assert await db_session.scalar(select(func.count()).select_from(ImportedStoryArcEntry)) == 26
    assert cancellation_checks >= 7
    summary = next(
        details for event, details in events if event == "import_story_arc_staging_completed"
    )
    assert summary["source_type"] == "filesystem"
    assert summary["arcs_staged"] == 1
    assert summary["entries_staged"] == 26
    assert summary["cohorts_examined"] == 1
    assert str(tmp_path) not in json.dumps(summary)
    resolution_summary = next(
        details for event, details in events if event == "import_story_arc_resolution_completed"
    )
    assert resolution_summary == {
        "message": "Staged story-arc entries resolved for review",
        "entries_examined": 26,
        "resolved": 0,
        "pending": 26,
        "missing": 0,
        "ambiguous": 0,
        "conflicts": 0,
        "skipped": 0,
        "linked_files": 0,
    }
    assert milestones == ["file_matching", "story_arc_resolution"]
    series_events = [event for event in progress_events if event.current_series]
    assert len(series_events) == 26
    assert all(event.current_item_progress_pct == 100 for event in series_events)
    assert all(event.estimated_seconds_remaining is None for event in series_events)
    assert str(tmp_path) not in json.dumps(resolution_summary)


def test_scan_failure_message_names_sqlite_lock_contention() -> None:
    exc = OperationalError("INSERT INTO import_job_logs", {}, Exception("database is locked"))

    message = _scan_failure_message(exc)

    assert "Database was busy while saving import progress" in message
    assert "WAL mode" in message


def test_scan_failure_message_preserves_non_lock_errors() -> None:
    assert _scan_failure_message(RuntimeError("archive exploded")) == "archive exploded"
