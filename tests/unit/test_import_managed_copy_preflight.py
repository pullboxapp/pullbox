"""Tests for managed-copy root and capacity preflight helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportFileHandlingMode,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series
from pullbox.models.story_arc import (
    ImportedStoryArcStatus,
    StoryArcResolutionState,
    StoryArcSourceKind,
)
from pullbox.models.story_arc_import import ImportedStoryArc, ImportedStoryArcEntry
from pullbox.services import import_managed_copy_preflight, library_root_management
from pullbox.services.import_managed_copy_preflight import (
    ManagedCopyPreflightError,
    ManagedCopyPreflightFailure,
    estimate_conversion_workspace_source_bytes,
    managed_copy_capacity_reserve,
    reopen_review_after_managed_copy_preflight_failure,
    selected_managed_copy_source_bytes,
    selected_story_arc_copy_source_bytes_by_root,
    validate_managed_copy_preflight,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def test_managed_copy_capacity_reserve_uses_fixed_minimum_then_ten_percent() -> None:
    assert managed_copy_capacity_reserve(0) == 1024**3
    assert managed_copy_capacity_reserve(5 * 1024**3) == 1024**3
    assert managed_copy_capacity_reserve(20 * 1024**3) == 2 * 1024**3


async def test_selected_source_bytes_counts_new_and_selected_duplicate_files(
    db_session: AsyncSession,
) -> None:
    job = ImportJob(
        source_path="/imports",
        source_type=ImportSourceType.FILESYSTEM,
    )
    db_session.add(job)
    await db_session.flush()
    confirmed = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Confirmed",
        status=ImportSeriesStatus.CONFIRMED,
    )
    duplicate = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Duplicate",
        status=ImportSeriesStatus.DUPLICATE,
    )
    db_session.add_all([confirmed, duplicate])
    await db_session.flush()
    db_session.add_all(
        [
            ImportedFile(
                import_job_id=job.id,
                import_series_id=confirmed.id,
                file_path="/imports/confirmed.cbz",
                file_name="confirmed.cbz",
                file_size=100,
                file_format="cbz",
                status=ImportedFileStatus.CONFIRMED,
            ),
            ImportedFile(
                import_job_id=job.id,
                import_series_id=duplicate.id,
                file_path="/imports/selected-duplicate.cbz",
                file_name="selected-duplicate.cbz",
                file_size=200,
                file_format="cbz",
                status=ImportedFileStatus.MATCHED,
                include_in_import=True,
            ),
            ImportedFile(
                import_job_id=job.id,
                import_series_id=duplicate.id,
                file_path="/imports/unselected-duplicate.cbz",
                file_name="unselected-duplicate.cbz",
                file_size=400,
                file_format="cbz",
                status=ImportedFileStatus.MATCHED,
                include_in_import=False,
            ),
        ]
    )
    await db_session.flush()

    assert await selected_managed_copy_source_bytes(db_session, job.id) == 300


async def test_reopen_review_restores_confirmed_story_arc_to_ready(
    db_session: AsyncSession,
) -> None:
    job = ImportJob(
        source_path="/imports",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.IMPORTING,
    )
    db_session.add(job)
    await db_session.flush()
    staged_arc = ImportedStoryArc(
        import_job_id=job.id,
        source_kind=StoryArcSourceKind.MYLAR3,
        source_key="mylar3:capacity",
        source_ordinal=1,
        name="Capacity Arc",
        status=ImportedStoryArcStatus.CONFIRMED,
        selected_for_import=True,
    )
    db_session.add(staged_arc)
    await db_session.flush()

    await reopen_review_after_managed_copy_preflight_failure(
        db_session,
        job,
        ManagedCopyPreflightError(
            ManagedCopyPreflightFailure.CAPACITY_UNKNOWN,
            "Available space could not be determined.",
        ),
    )

    await db_session.refresh(staged_arc)
    assert job.status == ImportJobStatus.REVIEW
    assert staged_arc.status == ImportedStoryArcStatus.READY
    assert staged_arc.selected_for_import is True


def _placement_policy(root_id: int, *, mode: str = "copy") -> dict[str, object]:
    return {
        "schema_version": 1,
        "source": "mylar3",
        "activation": "confirmed",
        "monitored": True,
        "search_missing": False,
        "include_upcoming": False,
        "sync_enabled": True,
        "placement_policy": {
            "schema_version": 1,
            "mode": mode,
            "target_library_root_id": root_id,
            "destination_root": "/story-arcs",
            "folder_template": "{StoryArc}",
            "file_template": "{ReadingOrder:03d} - {Series} #{IssueNumber}",
            "symlink_style": None,
            "synchronize": True,
        },
    }


async def _add_canonical_story_arc_entry(
    db_session: AsyncSession,
    *,
    job: ImportJob,
    root: LibraryRoot,
    arc_root_id: int,
    ordinal: int,
    file_size: int,
) -> ImportedStoryArc:
    series = Series(title=f"Capacity Series {ordinal}", sort_title=f"capacity series {ordinal}")
    db_session.add(series)
    await db_session.flush()
    issue = Issue(
        series_id=series.id,
        issue_number=float(ordinal),
        issue_number_text=str(ordinal),
    )
    db_session.add(issue)
    await db_session.flush()
    db_session.add(
        LibraryFile(
            file_path=f"{root.path}/capacity-{ordinal}.cbz",
            file_name=f"capacity-{ordinal}.cbz",
            file_size=file_size,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
            match_confidence=MatchConfidence.HIGH,
            issue_id=issue.id,
            library_root_id=root.id,
        )
    )
    staged_arc = ImportedStoryArc(
        import_job_id=job.id,
        source_kind=StoryArcSourceKind.MYLAR3,
        source_key=f"mylar3:capacity:{ordinal}",
        source_ordinal=ordinal,
        name=f"Capacity Arc {ordinal}",
        status=ImportedStoryArcStatus.CONFIRMED,
        selected_for_import=True,
        proposed_policy_snapshot=_placement_policy(arc_root_id),
    )
    db_session.add(staged_arc)
    await db_session.flush()
    db_session.add(
        ImportedStoryArcEntry(
            imported_story_arc_id=staged_arc.id,
            matched_issue_id=issue.id,
            source_ordinal=ordinal,
            reading_order=ordinal,
            resolution_state=StoryArcResolutionState.RESOLVED,
            source_kind=StoryArcSourceKind.MYLAR3,
            selected_for_import=True,
        )
    )
    await db_session.flush()
    return staged_arc


@pytest.mark.asyncio
async def test_arc_only_in_place_copy_counts_canonical_bytes_on_actual_arc_root(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    canonical_path = tmp_path / "canonical"
    arc_path = tmp_path / "arc"
    canonical_path.mkdir()
    arc_path.mkdir()
    canonical_root = LibraryRoot(name="Canonical", path=str(canonical_path), enabled=True)
    arc_root = LibraryRoot(name="Arc", path=str(arc_path), enabled=True)
    job = ImportJob(
        source_path="/imports/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.IMPORTING,
        file_handling_mode=ImportFileHandlingMode.IN_PLACE,
    )
    db_session.add_all([canonical_root, arc_root, job])
    await db_session.flush()
    await _add_canonical_story_arc_entry(
        db_session,
        job=job,
        root=canonical_root,
        arc_root_id=arc_root.id,
        ordinal=1,
        file_size=321,
    )

    assert await selected_story_arc_copy_source_bytes_by_root(db_session, job.id) == {
        arc_root.id: 321
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["hardlink", "symlink", "reference_only", "logical"])
async def test_story_arc_non_copy_modes_do_not_charge_copy_capacity(
    db_session: AsyncSession,
    tmp_path: Path,
    mode: str,
) -> None:
    canonical_path = tmp_path / "canonical"
    arc_path = tmp_path / "arc"
    canonical_path.mkdir()
    arc_path.mkdir()
    canonical_root = LibraryRoot(name="Canonical", path=str(canonical_path), enabled=True)
    arc_root = LibraryRoot(name="Arc", path=str(arc_path), enabled=True)
    job = ImportJob(
        source_path="/imports/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.IMPORTING,
        file_handling_mode=ImportFileHandlingMode.IN_PLACE,
    )
    db_session.add_all([canonical_root, arc_root, job])
    await db_session.flush()
    staged_arc = await _add_canonical_story_arc_entry(
        db_session,
        job=job,
        root=canonical_root,
        arc_root_id=arc_root.id,
        ordinal=1,
        file_size=321,
    )
    staged_arc.proposed_policy_snapshot = _placement_policy(arc_root.id, mode=mode)
    await db_session.flush()

    assert await selected_story_arc_copy_source_bytes_by_root(db_session, job.id) == {}


@pytest.mark.asyncio
async def test_story_arc_copy_uses_canonical_file_then_selected_job_file_fallback(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    canonical_path = tmp_path / "canonical"
    arc_path = tmp_path / "arc"
    canonical_path.mkdir()
    arc_path.mkdir()
    canonical_root = LibraryRoot(name="Canonical", path=str(canonical_path), enabled=True)
    arc_root = LibraryRoot(name="Arc", path=str(arc_path), enabled=True)
    job = ImportJob(
        source_path="/imports",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.IMPORTING,
    )
    db_session.add_all([canonical_root, arc_root, job])
    await db_session.flush()
    series = Series(title="Selected source", sort_title="selected source")
    db_session.add(series)
    await db_session.flush()
    issue = Issue(series_id=series.id, issue_number=1, issue_number_text="1")
    db_session.add(issue)
    await db_session.flush()
    canonical_file = LibraryFile(
        file_path=f"{canonical_root.path}/old.cbz",
        file_name="old.cbz",
        file_size=999,
        file_format=FileFormat.CBZ,
        file_modified_at=datetime.now(UTC),
        match_confidence=MatchConfidence.HIGH,
        issue_id=issue.id,
        library_root_id=canonical_root.id,
    )
    db_session.add(canonical_file)
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Selected source",
        status=ImportSeriesStatus.CONFIRMED,
    )
    db_session.add(imported_series)
    await db_session.flush()
    imported_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=imported_series.id,
        file_path="/imports/new.cbz",
        file_name="new.cbz",
        file_size=123,
        file_format="cbz",
        status=ImportedFileStatus.CONFIRMED,
        matched_issue_id=issue.id,
    )
    staged_arc = ImportedStoryArc(
        import_job_id=job.id,
        source_kind=StoryArcSourceKind.FOLDER,
        source_key="folder:selected-source",
        source_ordinal=1,
        name="Selected source arc",
        status=ImportedStoryArcStatus.CONFIRMED,
        selected_for_import=True,
        proposed_policy_snapshot=_placement_policy(arc_root.id),
    )
    db_session.add_all([imported_file, staged_arc])
    await db_session.flush()
    db_session.add(
        ImportedStoryArcEntry(
            imported_story_arc_id=staged_arc.id,
            import_file_id=imported_file.id,
            matched_issue_id=issue.id,
            source_ordinal=1,
            reading_order=1,
            resolution_state=StoryArcResolutionState.RESOLVED,
            source_kind=StoryArcSourceKind.FOLDER,
            selected_for_import=True,
        )
    )
    await db_session.flush()

    assert await selected_story_arc_copy_source_bytes_by_root(db_session, job.id) == {
        arc_root.id: 999
    }
    await db_session.delete(canonical_file)
    await db_session.flush()
    assert await selected_story_arc_copy_source_bytes_by_root(db_session, job.id) == {
        arc_root.id: 123
    }


@pytest.mark.asyncio
async def test_arc_copy_preflight_validates_each_actual_root_and_preserves_v2_details(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_path = tmp_path / "arc-one"
    second_path = tmp_path / "arc-two"
    first_path.mkdir()
    second_path.mkdir()
    first_root = LibraryRoot(name="Arc one", path=str(first_path), enabled=True)
    second_root = LibraryRoot(name="Arc two", path=str(second_path), enabled=True)
    job = ImportJob(
        source_path="/imports/mylar.db",
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.IMPORTING,
        file_handling_mode=ImportFileHandlingMode.IN_PLACE,
    )
    db_session.add_all([first_root, second_root, job])
    await db_session.flush()
    await _add_canonical_story_arc_entry(
        db_session,
        job=job,
        root=first_root,
        arc_root_id=first_root.id,
        ordinal=1,
        file_size=100,
    )
    await _add_canonical_story_arc_entry(
        db_session,
        job=job,
        root=second_root,
        arc_root_id=second_root.id,
        ordinal=2,
        file_size=200,
    )
    monkeypatch.setattr(
        library_root_management.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(
            free=(1024**3 + 99 if Path(path) == first_path else 1024**3 + 200)
        ),
    )

    with pytest.raises(ManagedCopyPreflightError) as error:
        await validate_managed_copy_preflight(db_session, job, stage="execution")

    assert error.value.reason is ManagedCopyPreflightFailure.CAPACITY_INSUFFICIENT
    assert error.value.snapshot is not None
    assert error.value.snapshot.target_library_root_id == first_root.id
    persisted = job.progress_snapshot["managed_copy_capacity"]
    assert persisted["schema_version"] == 2
    assert [item["target_library_root_id"] for item in persisted["target_capacities"]] == [
        first_root.id
    ]


@pytest.mark.asyncio
async def test_conversion_workspace_uses_largest_concurrent_non_cbz_sources(
    db_session: AsyncSession,
) -> None:
    job = ImportJob(
        source_path="/imports",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.IMPORTING,
        move_to_library=True,
        convert_to_preferred_format=True,
    )
    db_session.add(job)
    await db_session.flush()
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Conversion",
        status=ImportSeriesStatus.CONFIRMED,
    )
    db_session.add(imported_series)
    await db_session.flush()
    for ordinal, size, suffix in [
        (1, 100, "cbr"),
        (2, 300, "pdf"),
        (3, 200, "cb7"),
        (4, 1_000, "cbz"),
    ]:
        db_session.add(
            ImportedFile(
                import_job_id=job.id,
                import_series_id=imported_series.id,
                file_path=f"/imports/{ordinal}.{suffix}",
                file_name=f"{ordinal}.{suffix}",
                file_size=size,
                file_format=suffix,
                status=ImportedFileStatus.CONFIRMED,
            )
        )
    await db_session.flush()

    source_bytes, worker_count = await estimate_conversion_workspace_source_bytes(
        db_session,
        job,
        worker_count=2,
    )

    assert source_bytes == 500
    assert worker_count == 2


@pytest.mark.asyncio
async def test_conversion_workspace_capacity_is_checked_on_temp_filesystem(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path = tmp_path / "managed"
    temp_path = tmp_path / "temp"
    root_path.mkdir()
    temp_path.mkdir()
    root = LibraryRoot(name="Managed", path=str(root_path), enabled=True)
    job = ImportJob(
        source_path="/imports",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.IMPORTING,
        target_library_root_id=None,
        move_to_library=True,
        convert_to_preferred_format=True,
    )
    db_session.add_all([root, job])
    await db_session.flush()
    job.target_library_root_id = root.id
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Conversion",
        status=ImportSeriesStatus.CONFIRMED,
    )
    db_session.add(imported_series)
    await db_session.flush()
    db_session.add(
        ImportedFile(
            import_job_id=job.id,
            import_series_id=imported_series.id,
            file_path="/imports/large.cbr",
            file_name="large.cbr",
            file_size=600,
            file_format="cbr",
            status=ImportedFileStatus.CONFIRMED,
        )
    )
    await db_session.flush()
    monkeypatch.setattr(
        library_root_management.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=100 * 1024**3),
    )
    monkeypatch.setattr(
        import_managed_copy_preflight.tempfile,
        "gettempdir",
        lambda: str(temp_path),
    )
    monkeypatch.setattr(
        import_managed_copy_preflight.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=1024**3 + 1_199),
    )

    with pytest.raises(ManagedCopyPreflightError) as error:
        await validate_managed_copy_preflight(db_session, job, stage="execution")

    assert error.value.reason is ManagedCopyPreflightFailure.CAPACITY_INSUFFICIENT
    assert error.value.snapshot is not None
    assert error.value.snapshot.target_library_root_id is None
    persisted = job.progress_snapshot["managed_copy_capacity"]
    assert persisted["conversion_workspace"]["estimated_workspace_bytes"] == 1_200
    assert persisted["conversion_workspace"]["status"] == "insufficient"
