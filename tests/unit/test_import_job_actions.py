"""Tests for import action journal helpers."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from sqlalchemy.dialects import postgresql

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.core.exceptions import NotFoundError
from pullbox.core.library_file_ownership import build_managed_placement_signature
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobAction,
    ImportJobActionStatus,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series
from pullbox.services.import_job_actions import (
    _series_issue_rollback_lock_statement,
    _series_rollback_lock_statement,
    build_series_created_action_payload,
)
from pullbox.services.import_rollback_execution import RollbackActionPlan
from pullbox.services.import_service import ImportService

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


async def _create_job_row(session: AsyncSession) -> ImportJob:
    job = ImportJob(
        source_path="/tmp/comics",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.IMPORTING,
    )
    session.add(job)
    await session.flush()
    return job


def _make_service() -> ImportService:
    return ImportService(
        series_service=AsyncMock(),
        metadata_service=AsyncMock(),
        event_bus=AsyncMock(),
    )


def _rollback_plan(action: ImportJobAction) -> RollbackActionPlan:
    return RollbackActionPlan(
        action_id=action.id,
        sequence_no=action.sequence_no,
        action_type=action.action_type,
        payload=dict(action.payload or {}),
    )


def test_series_created_rollback_locks_series_and_issues_for_postgresql() -> None:
    dialect = postgresql.dialect()
    series_sql = str(_series_rollback_lock_statement(42).compile(dialect=dialect))
    issue_sql = str(_series_issue_rollback_lock_statement(42).compile(dialect=dialect))

    assert "FOR UPDATE" in series_sql
    assert "FOR UPDATE" in issue_sql
    assert "ORDER BY issues.id" in issue_sql


async def test_next_action_sequence_uses_highest_existing_sequence(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    db_session.add_all(
        [
            ImportJobAction(
                import_job_id=job.id,
                sequence_no=1,
                phase="import",
                action_type="series_created",
                status=ImportJobActionStatus.COMPLETED,
                payload={"series_id": 10},
            ),
            ImportJobAction(
                import_job_id=job.id,
                sequence_no=4,
                phase="import",
                action_type="library_file_registered",
                status=ImportJobActionStatus.COMPLETED,
                payload={"library_file_id": 20},
            ),
        ]
    )
    await db_session.flush()

    assert await service._next_action_sequence(db_session, job.id) == 5


async def test_record_action_persists_completed_action_with_next_sequence(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)

    action = await service._record_action(
        db_session,
        job,
        phase="import",
        action_type="series_created",
        payload={"series_id": 123, "import_series_id": 456},
    )

    assert action.import_job_id == job.id
    assert action.sequence_no == 1
    assert action.phase == "import"
    assert action.action_type == "series_created"
    assert action.status == ImportJobActionStatus.COMPLETED
    assert action.payload == {"series_id": 123, "import_series_id": 456}
    assert await service._next_action_sequence(db_session, job.id) == 2


async def test_rollback_action_removes_copied_library_file_and_journal_row(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    destination_path = tmp_path / "library" / "Absolute Wonder Woman 019.cbz"
    destination_path.parent.mkdir(parents=True)
    destination_path.write_text("comic", encoding="utf-8")
    original_path = tmp_path / "incoming" / "Absolute Wonder Woman 019.cbz"
    root = LibraryRoot(name="Library", path=str(tmp_path / "library"))
    db_session.add(root)
    await db_session.flush()
    library_file = LibraryFile(
        file_path=str(destination_path),
        file_name=destination_path.name,
        file_size=destination_path.stat().st_size,
        file_format=FileFormat.CBZ,
        file_hash=None,
        file_modified_at=datetime.now(tz=UTC),
        match_confidence=MatchConfidence.HIGH,
        issue_id=None,
        library_root_id=root.id,
    )
    db_session.add(library_file)
    await db_session.flush()
    action = ImportJobAction(
        import_job_id=job.id,
        sequence_no=1,
        phase="import",
        action_type="library_file_registered",
        status=ImportJobActionStatus.COMPLETED,
        payload={
            "library_file_id": library_file.id,
            "destination_path": str(destination_path),
            "destination_signature": build_managed_placement_signature(destination_path),
            "original_source_path": str(original_path),
            "transfer_method": "copy",
        },
    )
    db_session.add(action)
    await db_session.flush()

    await service._rollback_action(db_session, _rollback_plan(action))

    assert await db_session.get(LibraryFile, library_file.id) is None
    assert not destination_path.exists()
    assert action.status == ImportJobActionStatus.ROLLED_BACK
    assert action.rolled_back_at is not None


async def test_rollback_action_preserves_managed_file_changed_after_registration(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    destination_path = tmp_path / "library" / "Absolute Wonder Woman 019.cbz"
    destination_path.parent.mkdir(parents=True)
    original_content = b"import-created comic"
    destination_path.write_bytes(original_content)
    creation_signature = build_managed_placement_signature(destination_path)
    creation_stat = destination_path.stat()
    original_path = tmp_path / "incoming" / destination_path.name
    root = LibraryRoot(name="Library", path=str(destination_path.parent))
    db_session.add(root)
    await db_session.flush()
    library_file = LibraryFile(
        file_path=str(destination_path),
        file_name=destination_path.name,
        file_size=destination_path.stat().st_size,
        file_format=FileFormat.CBZ,
        file_hash=None,
        file_modified_at=datetime.now(tz=UTC),
        match_confidence=MatchConfidence.HIGH,
        issue_id=None,
        library_root_id=root.id,
        source_signature=creation_signature,
    )
    db_session.add(library_file)
    await db_session.flush()
    action = ImportJobAction(
        import_job_id=job.id,
        sequence_no=1,
        phase="import",
        action_type="library_file_registered",
        status=ImportJobActionStatus.COMPLETED,
        payload={
            "library_file_id": library_file.id,
            "destination_path": str(destination_path),
            "destination_signature": creation_signature,
            "original_source_path": str(original_path),
            "transfer_method": "copy",
            "storage_mode": "managed",
        },
    )
    db_session.add(action)
    await db_session.flush()
    changed_content = b"x" * len(original_content)
    destination_path.write_bytes(changed_content)
    os.utime(
        destination_path,
        ns=(creation_stat.st_atime_ns, creation_stat.st_mtime_ns),
    )

    await service._rollback_action(db_session, _rollback_plan(action))

    assert destination_path.read_bytes() == changed_content
    assert await db_session.get(LibraryFile, library_file.id) is library_file
    assert action.status == ImportJobActionStatus.ROLLBACK_FAILED
    assert action.rolled_back_at is None
    assert "changed after import" in (action.error_message or "")


async def test_rollback_action_detaches_referenced_file_without_touching_source(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    source_path = tmp_path / "library" / "Existing Series" / "Issue 001.cbz"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("user-owned comic", encoding="utf-8")
    stale_original_path = tmp_path / "incoming" / source_path.name
    root = LibraryRoot(name="Library", path=str(tmp_path / "library"))
    db_session.add(root)
    await db_session.flush()
    library_file = LibraryFile(
        file_path=str(source_path),
        file_name=source_path.name,
        file_size=source_path.stat().st_size,
        file_format=FileFormat.CBZ,
        file_modified_at=datetime.now(tz=UTC),
        match_confidence=MatchConfidence.HIGH,
        library_root_id=root.id,
    )
    db_session.add(library_file)
    await db_session.flush()
    action = ImportJobAction(
        import_job_id=job.id,
        sequence_no=1,
        phase="import",
        action_type="library_file_registered",
        status=ImportJobActionStatus.COMPLETED,
        payload={
            "library_file_id": library_file.id,
            "destination_path": str(source_path),
            "original_source_path": str(stale_original_path),
            "transfer_method": "leave_in_place",
            "storage_mode": "referenced",
        },
    )
    db_session.add(action)
    await db_session.flush()

    await service._rollback_action(db_session, _rollback_plan(action))

    assert await db_session.get(LibraryFile, library_file.id) is None
    assert source_path.read_text(encoding="utf-8") == "user-owned comic"
    assert not stale_original_path.exists()
    assert action.status == ImportJobActionStatus.ROLLED_BACK


async def test_rollback_action_deletes_created_series_without_files(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    series_folder = tmp_path / "library" / "New Series"
    series_folder.mkdir(parents=True)
    series = Series(
        title="New Series",
        sort_title="new series",
        comicvine_id=321,
        path=str(series_folder),
    )
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="New Series",
        status=ImportSeriesStatus.IMPORTED,
        series=series,
    )
    db_session.add(imported_series)
    await db_session.flush()
    action = ImportJobAction(
        import_job_id=job.id,
        sequence_no=1,
        phase="import",
        action_type="series_created",
        status=ImportJobActionStatus.COMPLETED,
        payload=await build_series_created_action_payload(
            db_session,
            series_id=series.id,
            import_series_id=imported_series.id,
        ),
    )
    db_session.add(action)
    await db_session.flush()

    await service._rollback_action(db_session, _rollback_plan(action))

    service._series_service.delete.assert_awaited_once_with(
        db_session,
        series.id,
        delete_files=False,
        delete_folder=False,
    )
    assert series_folder.exists()
    assert action.status == ImportJobActionStatus.ROLLED_BACK
    assert action.rolled_back_at is not None


async def test_rollback_action_tolerates_missing_created_series(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    service._series_service.delete.side_effect = NotFoundError("Series", 321)
    job = await _create_job_row(db_session)
    action = ImportJobAction(
        import_job_id=job.id,
        sequence_no=1,
        phase="import",
        action_type="series_created",
        status=ImportJobActionStatus.COMPLETED,
        payload={"series_id": 321},
    )
    db_session.add(action)
    await db_session.flush()

    await service._rollback_action(db_session, _rollback_plan(action))

    service._series_service.delete.assert_not_awaited()
    assert action.status == ImportJobActionStatus.ROLLED_BACK


async def test_rollback_action_preserves_created_series_changed_after_import(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    series = Series(
        title="New Series",
        sort_title="new series",
        comicvine_id=322,
        monitored=False,
    )
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="New Series",
        status=ImportSeriesStatus.IMPORTED,
        series=series,
    )
    db_session.add(imported_series)
    await db_session.flush()
    payload = await build_series_created_action_payload(
        db_session,
        series_id=series.id,
        import_series_id=imported_series.id,
    )
    action = ImportJobAction(
        import_job_id=job.id,
        sequence_no=1,
        phase="import",
        action_type="series_created",
        status=ImportJobActionStatus.COMPLETED,
        payload=payload,
    )
    db_session.add(action)
    await db_session.flush()

    series.monitored = True
    await db_session.flush()

    await service._rollback_action(db_session, _rollback_plan(action))

    assert await db_session.get(Series, series.id) is series
    service._series_service.delete.assert_not_awaited()
    assert action.status == ImportJobActionStatus.ROLLBACK_FAILED
    assert action.rolled_back_at is None
    assert "changed after import" in (action.error_message or "")


async def test_rollback_action_preserves_created_series_with_later_issue_state(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    series = Series(
        title="New Series",
        sort_title="new series",
        comicvine_id=324,
        monitored=False,
    )
    issue = Issue(
        series=series,
        issue_number=1,
        issue_number_text="1",
        status=IssueStatus.SKIPPED,
    )
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="New Series",
        status=ImportSeriesStatus.IMPORTED,
        series=series,
    )
    db_session.add_all([issue, imported_series])
    await db_session.flush()
    payload = await build_series_created_action_payload(
        db_session,
        series_id=series.id,
        import_series_id=imported_series.id,
    )
    action = ImportJobAction(
        import_job_id=job.id,
        sequence_no=1,
        phase="import",
        action_type="series_created",
        status=ImportJobActionStatus.COMPLETED,
        payload=payload,
    )
    db_session.add(action)
    await db_session.flush()

    issue.status = IssueStatus.WANTED
    await db_session.flush()

    await service._rollback_action(db_session, _rollback_plan(action))

    assert await db_session.get(Series, series.id) is series
    service._series_service.delete.assert_not_awaited()
    assert action.status == ImportJobActionStatus.ROLLBACK_FAILED
    assert "issue" in (action.error_message or "")


async def test_rollback_action_allows_issue_state_owned_by_same_import(
    db_session: AsyncSession,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    series = Series(
        title="New Series",
        sort_title="new series",
        comicvine_id=325,
        monitored=False,
    )
    issue = Issue(
        series=series,
        issue_number=1,
        issue_number_text="1",
        status=IssueStatus.SKIPPED,
    )
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="New Series",
        status=ImportSeriesStatus.IMPORTED,
        series=series,
    )
    db_session.add_all([issue, imported_series])
    await db_session.flush()
    imported_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=imported_series.id,
        file_path="/imports/New Series 001.cbz",
        file_name="New Series 001.cbz",
        file_size=1,
        file_format="cbz",
        status=ImportedFileStatus.PENDING,
        matched_issue_id=issue.id,
    )
    db_session.add(imported_file)
    await db_session.flush()
    payload = await build_series_created_action_payload(
        db_session,
        series_id=series.id,
        import_series_id=imported_series.id,
    )
    action = ImportJobAction(
        import_job_id=job.id,
        sequence_no=1,
        phase="import",
        action_type="series_created",
        status=ImportJobActionStatus.COMPLETED,
        payload=payload,
    )
    db_session.add(action)
    issue.status = IssueStatus.OWNED
    imported_file.status = ImportedFileStatus.IMPORTED
    await db_session.flush()

    await service._rollback_action(db_session, _rollback_plan(action))

    service._series_service.delete.assert_awaited_once_with(
        db_session,
        series.id,
        delete_files=False,
        delete_folder=False,
    )
    assert action.status == ImportJobActionStatus.ROLLED_BACK


async def test_rollback_action_preserves_created_series_with_later_file_and_download(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    root_path = tmp_path / "library"
    series_folder = root_path / "New Series"
    series_folder.mkdir(parents=True)
    root = LibraryRoot(name="Library", path=str(root_path))
    series = Series(
        title="New Series",
        sort_title="new series",
        comicvine_id=323,
        path=str(series_folder),
        library_root=root,
    )
    issue = Issue(series=series, issue_number=1, issue_number_text="1")
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="New Series",
        status=ImportSeriesStatus.IMPORTED,
        series=series,
    )
    db_session.add_all([root, issue, imported_series])
    await db_session.flush()
    payload = await build_series_created_action_payload(
        db_session,
        series_id=series.id,
        import_series_id=imported_series.id,
    )
    action = ImportJobAction(
        import_job_id=job.id,
        sequence_no=1,
        phase="import",
        action_type="series_created",
        status=ImportJobActionStatus.COMPLETED,
        payload=payload,
    )
    later_path = series_folder / "New Series 001.cbz"
    later_path.write_bytes(b"later user-owned file")
    library_file = LibraryFile(
        file_path=str(later_path),
        file_name=later_path.name,
        file_size=later_path.stat().st_size,
        file_format=FileFormat.CBZ,
        file_modified_at=datetime.now(UTC),
        match_confidence=MatchConfidence.HIGH,
        issue=issue,
        library_root=root,
    )
    download = DownloadHistory(
        issue=issue,
        title="New Series 001",
        download_url="https://example.invalid/new-series-001",
        download_client=DownloadClientType.NZBGET,
        protocol=AcquisitionProtocol.USENET,
        state=DownloadState.DOWNLOADING,
    )
    db_session.add_all([action, library_file, download])
    await db_session.flush()

    await service._rollback_action(db_session, _rollback_plan(action))

    assert await db_session.get(Series, series.id) is series
    assert await db_session.get(LibraryFile, library_file.id) is library_file
    assert await db_session.get(DownloadHistory, download.id) is download
    assert download.state == DownloadState.DOWNLOADING
    assert later_path.read_bytes() == b"later user-owned file"
    service._series_service.delete.assert_not_awaited()
    assert action.status == ImportJobActionStatus.ROLLBACK_FAILED
    assert "later files or activity" in (action.error_message or "")


async def test_rollback_action_restores_series_preferred_future_root(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    old_path = tmp_path / "old-root"
    new_path = tmp_path / "new-root"
    old_path.mkdir()
    new_path.mkdir()
    old_root = LibraryRoot(name="Old", path=str(old_path))
    new_root = LibraryRoot(name="New", path=str(new_path))
    db_session.add_all([old_root, new_root])
    await db_session.flush()
    series = Series(
        title="Batman",
        sort_title="batman",
        year_start=2011,
        comicvine_id=796,
        preferred_library_root_id=new_root.id,
    )
    db_session.add(series)
    await db_session.flush()
    action = ImportJobAction(
        import_job_id=job.id,
        sequence_no=1,
        phase="import",
        action_type="series_preferred_root_updated",
        status=ImportJobActionStatus.COMPLETED,
        payload={
            "series_id": series.id,
            "old_preferred_library_root_id": old_root.id,
            "new_preferred_library_root_id": new_root.id,
        },
    )
    db_session.add(action)
    await db_session.flush()

    await service._rollback_action(db_session, _rollback_plan(action))

    assert series.preferred_library_root_id == old_root.id
    assert action.status == ImportJobActionStatus.ROLLED_BACK
    assert action.rolled_back_at is not None


async def test_rollback_action_preserves_concurrently_changed_preferred_future_root(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    root_specs = (
        ("Old", "old-root"),
        ("Imported", "imported-root"),
        ("User choice", "user-root"),
    )
    for _name, slug in root_specs:
        (tmp_path / slug).mkdir()
    roots = [LibraryRoot(name=name, path=str(tmp_path / slug)) for name, slug in root_specs]
    db_session.add_all(roots)
    await db_session.flush()
    old_root, imported_root, user_root = roots
    series = Series(
        title="Batman",
        sort_title="batman",
        comicvine_id=796,
        preferred_library_root_id=user_root.id,
    )
    db_session.add(series)
    await db_session.flush()
    action = ImportJobAction(
        import_job_id=job.id,
        sequence_no=1,
        phase="import",
        action_type="series_preferred_root_updated",
        status=ImportJobActionStatus.COMPLETED,
        payload={
            "series_id": series.id,
            "old_preferred_library_root_id": old_root.id,
            "new_preferred_library_root_id": imported_root.id,
        },
    )
    db_session.add(action)
    await db_session.flush()

    await service._rollback_action(db_session, _rollback_plan(action))

    assert series.preferred_library_root_id == user_root.id
    assert action.status == ImportJobActionStatus.ROLLBACK_FAILED
    assert action.rolled_back_at is None
    assert "preserved the current choice" in (action.error_message or "")


async def test_rollback_action_removes_empty_import_created_series_folder(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    series_folder = tmp_path / "library" / "New Series (2026)"
    series_folder.mkdir(parents=True)
    destination_path = series_folder / "New Series 001.cbz"
    destination_path.write_text("comic", encoding="utf-8")
    original_path = tmp_path / "incoming" / "New Series 001.cbz"
    original_path.parent.mkdir(parents=True)
    original_path.write_text("source", encoding="utf-8")
    root = LibraryRoot(name="Library", path=str(tmp_path / "library"))
    db_session.add(root)
    await db_session.flush()
    library_file = LibraryFile(
        file_path=str(destination_path),
        file_name=destination_path.name,
        file_size=destination_path.stat().st_size,
        file_format=FileFormat.CBZ,
        file_hash=None,
        file_modified_at=datetime.now(tz=UTC),
        match_confidence=MatchConfidence.HIGH,
        issue_id=None,
        library_root_id=root.id,
    )
    db_session.add(library_file)
    await db_session.flush()
    action = ImportJobAction(
        import_job_id=job.id,
        sequence_no=1,
        phase="import",
        action_type="library_file_registered",
        status=ImportJobActionStatus.COMPLETED,
        payload={
            "library_file_id": library_file.id,
            "destination_path": str(destination_path),
            "destination_signature": build_managed_placement_signature(destination_path),
            "original_source_path": str(original_path),
            "transfer_method": "copy",
            "created_series_folder": True,
            "created_series_folder_path": str(series_folder),
        },
    )
    db_session.add(action)
    await db_session.flush()

    await service._rollback_action(db_session, _rollback_plan(action))

    assert not destination_path.exists()
    assert not series_folder.exists()


async def test_rollback_action_preserves_preexisting_series_folder(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    series_folder = tmp_path / "library" / "Existing Series (2020)"
    series_folder.mkdir(parents=True)
    destination_path = series_folder / "Existing Series 001.cbz"
    destination_path.write_text("comic", encoding="utf-8")
    original_path = tmp_path / "incoming" / "Existing Series 001.cbz"
    root = LibraryRoot(name="Library", path=str(tmp_path / "library"))
    db_session.add(root)
    await db_session.flush()
    library_file = LibraryFile(
        file_path=str(destination_path),
        file_name=destination_path.name,
        file_size=destination_path.stat().st_size,
        file_format=FileFormat.CBZ,
        file_hash=None,
        file_modified_at=datetime.now(tz=UTC),
        match_confidence=MatchConfidence.HIGH,
        issue_id=None,
        library_root_id=root.id,
    )
    db_session.add(library_file)
    await db_session.flush()
    action = ImportJobAction(
        import_job_id=job.id,
        sequence_no=1,
        phase="import",
        action_type="library_file_registered",
        status=ImportJobActionStatus.COMPLETED,
        payload={
            "library_file_id": library_file.id,
            "destination_path": str(destination_path),
            "destination_signature": build_managed_placement_signature(destination_path),
            "original_source_path": str(original_path),
            "transfer_method": "copy",
            "created_series_folder": False,
            "created_series_folder_path": str(series_folder),
        },
    )
    db_session.add(action)
    await db_session.flush()

    await service._rollback_action(db_session, _rollback_plan(action))

    assert series_folder.exists()


async def test_rollback_action_removes_unchanged_completed_placement_and_empty_folder(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    original_path = tmp_path / "incoming" / "Batwoman 002.cbz"
    original_path.parent.mkdir(parents=True)
    original_path.write_text("source", encoding="utf-8")
    series_folder = tmp_path / "library" / "Batwoman (2026)"
    series_folder.mkdir(parents=True)
    destination_path = series_folder / "Batwoman (2026) #002.cbz"
    destination_path.write_text("rewritten target", encoding="utf-8")
    destination_signature = build_managed_placement_signature(destination_path)
    action = ImportJobAction(
        import_job_id=job.id,
        sequence_no=1,
        phase="import",
        action_type="library_file_placement_started",
        status=ImportJobActionStatus.COMPLETED,
        payload={
            "destination_path": str(destination_path),
            "original_source_path": str(original_path),
            "artifact_source_path": str(original_path),
            "transfer_method": "copy",
            "created_series_folder": True,
            "created_series_folder_path": str(series_folder),
            "placement_completed": True,
            "destination_signature": destination_signature,
            "temp_paths": [],
        },
    )
    db_session.add(action)
    await db_session.flush()

    await service._rollback_action(db_session, _rollback_plan(action))

    assert original_path.exists()
    assert not destination_path.exists()
    assert not series_folder.exists()
    assert action.status == ImportJobActionStatus.ROLLED_BACK
    assert action.rolled_back_at is not None


async def test_rollback_action_restores_partial_move_when_source_was_removed(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    original_path = tmp_path / "incoming" / "Saga 001.cbz"
    original_path.parent.mkdir(parents=True)
    series_folder = tmp_path / "library" / "Saga (2012)"
    series_folder.mkdir(parents=True)
    destination_path = series_folder / "Saga (2012) #001.cbz"
    destination_path.write_text("moved target", encoding="utf-8")
    destination_signature = build_managed_placement_signature(destination_path)
    action = ImportJobAction(
        import_job_id=job.id,
        sequence_no=1,
        phase="import",
        action_type="library_file_placement_started",
        status=ImportJobActionStatus.COMPLETED,
        payload={
            "destination_path": str(destination_path),
            "original_source_path": str(original_path),
            "artifact_source_path": str(original_path),
            "transfer_method": "move",
            "created_series_folder": True,
            "created_series_folder_path": str(series_folder),
            "placement_completed": True,
            "destination_signature": destination_signature,
        },
    )
    db_session.add(action)
    await db_session.flush()

    await service._rollback_action(db_session, _rollback_plan(action))

    assert original_path.read_text(encoding="utf-8") == "moved target"
    assert not destination_path.exists()
    assert not series_folder.exists()
    assert action.status == ImportJobActionStatus.ROLLED_BACK
    assert action.rolled_back_at is not None


async def test_rollback_action_preserves_changed_completed_placement_for_review(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    original_path = tmp_path / "incoming" / "Saga 001.cbz"
    original_path.parent.mkdir(parents=True)
    original_path.write_text("source", encoding="utf-8")
    destination_path = tmp_path / "library" / "Saga (2012)" / "Saga 001.cbz"
    destination_path.parent.mkdir(parents=True)
    destination_path.write_text("import-created", encoding="utf-8")
    destination_signature = build_managed_placement_signature(destination_path)
    action = ImportJobAction(
        import_job_id=job.id,
        sequence_no=1,
        phase="import",
        action_type="library_file_placement_started",
        status=ImportJobActionStatus.COMPLETED,
        payload={
            "destination_path": str(destination_path),
            "original_source_path": str(original_path),
            "artifact_source_path": str(original_path),
            "transfer_method": "copy",
            "created_series_folder": True,
            "created_series_folder_path": str(destination_path.parent),
            "placement_completed": True,
            "destination_signature": destination_signature,
            "temp_paths": [],
        },
    )
    db_session.add(action)
    await db_session.flush()
    destination_path.write_text("changed after placement", encoding="utf-8")

    await service._rollback_action(db_session, _rollback_plan(action))

    assert destination_path.read_text(encoding="utf-8") == "changed after placement"
    assert original_path.read_text(encoding="utf-8") == "source"
    assert action.status == ImportJobActionStatus.ROLLBACK_FAILED
    assert action.rolled_back_at is None
    assert "preserved" in (action.error_message or "")


async def test_rollback_completed_move_preserves_reappeared_source_and_destination(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    original_path = tmp_path / "incoming" / "Saga 001.cbz"
    original_path.parent.mkdir(parents=True)
    original_path.write_text("reappeared source", encoding="utf-8")
    destination_path = tmp_path / "library" / "Saga (2012)" / "Saga 001.cbz"
    destination_path.parent.mkdir(parents=True)
    destination_path.write_text("owned destination", encoding="utf-8")
    action = ImportJobAction(
        import_job_id=job.id,
        sequence_no=1,
        phase="import",
        action_type="library_file_placement_started",
        status=ImportJobActionStatus.COMPLETED,
        payload={
            "destination_path": str(destination_path),
            "original_source_path": str(original_path),
            "artifact_source_path": str(original_path),
            "transfer_method": "move",
            "created_series_folder": True,
            "created_series_folder_path": str(destination_path.parent),
            "placement_completed": True,
            "destination_signature": build_managed_placement_signature(destination_path),
            "temp_paths": [],
        },
    )
    db_session.add(action)
    await db_session.flush()

    await service._rollback_action(db_session, _rollback_plan(action))

    assert original_path.read_text(encoding="utf-8") == "reappeared source"
    assert destination_path.read_text(encoding="utf-8") == "owned destination"
    assert action.status == ImportJobActionStatus.ROLLBACK_FAILED
    assert action.rolled_back_at is None


async def test_rollback_action_preserves_unproven_temp_artifact_for_review(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    original_path = tmp_path / "incoming" / "Saga 001.cbz"
    original_path.parent.mkdir(parents=True)
    original_path.write_text("source", encoding="utf-8")
    series_folder = tmp_path / "library" / "Saga (2012)"
    series_folder.mkdir(parents=True)
    temp_path = series_folder / ".Saga 001.cbz.pullbox-import-42-deadbeef.tmp"
    temp_path.write_text("unproven partial", encoding="utf-8")
    action = ImportJobAction(
        import_job_id=job.id,
        sequence_no=1,
        phase="import",
        action_type="library_file_placement_started",
        status=ImportJobActionStatus.COMPLETED,
        payload={
            "destination_path": str(series_folder / "Saga 001.cbz"),
            "original_source_path": str(original_path),
            "artifact_source_path": str(original_path),
            "transfer_method": "copy",
            "created_series_folder": True,
            "created_series_folder_path": str(series_folder),
            "placement_completed": False,
            "temp_paths": [str(temp_path)],
        },
    )
    db_session.add(action)
    await db_session.flush()

    await service._rollback_action(db_session, _rollback_plan(action))

    assert temp_path.read_text(encoding="utf-8") == "unproven partial"
    assert original_path.read_text(encoding="utf-8") == "source"
    assert action.status == ImportJobActionStatus.ROLLBACK_FAILED
    assert action.rolled_back_at is None


async def test_rollback_action_restores_adopted_import_file_paths(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    old_folder = tmp_path / "library" / "Mylar Folder"
    new_folder = tmp_path / "library" / "Canonical Series (2024)"
    new_folder.mkdir(parents=True)
    (new_folder / "cover.jpg").write_text("sidecar", encoding="utf-8")
    renamed_file = new_folder / "Mylar Series 001.cbz"
    renamed_file.write_text("comic", encoding="utf-8")
    series = Series(
        title="Canonical Series",
        sort_title="Canonical Series",
        year_start=2024,
        path=str(new_folder),
    )
    db_session.add(series)
    await db_session.flush()
    imported_series = ImportedSeries(
        import_job_id=job.id,
        status=ImportSeriesStatus.CONFIRMED,
        raw_series_name="Mylar Series",
        file_count=1,
        files_total=1,
        source_folder=str(old_folder),
        series_id=series.id,
    )
    db_session.add(imported_series)
    await db_session.flush()
    imported_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=imported_series.id,
        file_path=str(renamed_file),
        file_name=renamed_file.name,
        file_size=renamed_file.stat().st_size,
        file_format="cbz",
        status=ImportedFileStatus.MATCHED,
        include_in_import=True,
    )
    action = ImportJobAction(
        import_job_id=job.id,
        sequence_no=1,
        phase="import",
        action_type="series_folder_renamed",
        status=ImportJobActionStatus.COMPLETED,
        payload={
            "series_id": series.id,
            "import_series_id": imported_series.id,
            "old_folder_path": str(old_folder),
            "new_folder_path": str(new_folder),
            "old_series_path": str(old_folder),
            "old_library_root_id": None,
        },
    )
    db_session.add_all([imported_file, action])
    await db_session.flush()

    await service._rollback_action(db_session, _rollback_plan(action))

    restored_file_path = old_folder / "Mylar Series 001.cbz"
    assert old_folder.exists()
    assert not new_folder.exists()
    assert restored_file_path.exists()
    assert imported_file.file_path == str(restored_file_path)
    assert series.path == str(old_folder)
    assert action.status == ImportJobActionStatus.ROLLED_BACK
    assert action.rolled_back_at is not None


async def test_rollback_action_preserves_source_when_partial_destination_is_same_path(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    original_path = tmp_path / "library" / "Saga (2012)" / "Saga (2012) #001.cbz"
    original_path.parent.mkdir(parents=True)
    original_path.write_text("already in place", encoding="utf-8")
    action = ImportJobAction(
        import_job_id=job.id,
        sequence_no=1,
        phase="import",
        action_type="library_file_placement_started",
        status=ImportJobActionStatus.COMPLETED,
        payload={
            "destination_path": str(original_path),
            "original_source_path": str(original_path),
            "artifact_source_path": str(original_path),
            "transfer_method": "move",
            "created_series_folder": False,
            "created_series_folder_path": str(original_path.parent),
        },
    )
    db_session.add(action)
    await db_session.flush()

    await service._rollback_action(db_session, _rollback_plan(action))

    assert original_path.read_text(encoding="utf-8") == "already in place"
    assert action.status == ImportJobActionStatus.ROLLED_BACK
    assert action.rolled_back_at is not None


async def test_rollback_action_restores_surviving_permission_mode(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    source_path = tmp_path / "incoming" / "Saga 001.cbz"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("comic", encoding="utf-8")
    source_path.chmod(0o600)
    destination_path = tmp_path / "library" / "Saga (2012)" / "Saga 001.cbz"
    destination_path.parent.mkdir(parents=True)
    destination_path.hardlink_to(source_path)
    destination_path.chmod(0o640)
    root = LibraryRoot(name="Library", path=str(tmp_path / "library"))
    db_session.add(root)
    await db_session.flush()
    library_file = LibraryFile(
        file_path=str(destination_path),
        file_name=destination_path.name,
        file_size=destination_path.stat().st_size,
        file_format=FileFormat.CBZ,
        file_hash=None,
        file_modified_at=datetime.now(tz=UTC),
        match_confidence=MatchConfidence.HIGH,
        issue_id=None,
        library_root_id=root.id,
    )
    db_session.add(library_file)
    await db_session.flush()
    action = ImportJobAction(
        import_job_id=job.id,
        sequence_no=1,
        phase="import",
        action_type="library_file_registered",
        status=ImportJobActionStatus.COMPLETED,
        payload={
            "library_file_id": library_file.id,
            "destination_path": str(destination_path),
            "destination_signature": build_managed_placement_signature(destination_path),
            "original_source_path": str(source_path),
            "transfer_method": "hardlink",
            "permission_restores": [{"path": str(source_path), "mode": 0o600}],
        },
    )
    db_session.add(action)
    await db_session.flush()

    await service._rollback_action(db_session, _rollback_plan(action))

    assert not destination_path.exists()
    assert source_path.exists()
    assert source_path.stat().st_mode & 0o777 == 0o600


async def test_rollback_action_restores_adopted_series_folder_name(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    service = _make_service()
    job = await _create_job_row(db_session)
    old_folder = tmp_path / "library" / "Mylar Folder"
    new_folder = tmp_path / "library" / "Canonical Folder (2026)"
    new_folder.mkdir(parents=True)
    (new_folder / "series.json").write_text("mylar sidecar", encoding="utf-8")
    root = LibraryRoot(name="Library", path=str(tmp_path / "library"))
    series = Series(
        title="Canonical Folder",
        sort_title="canonical folder",
        year_start=2026,
        path=str(new_folder),
    )
    db_session.add_all([root, series])
    await db_session.flush()
    action = ImportJobAction(
        import_job_id=job.id,
        sequence_no=1,
        phase="import",
        action_type="series_folder_renamed",
        status=ImportJobActionStatus.COMPLETED,
        payload={
            "series_id": series.id,
            "old_folder_path": str(old_folder),
            "new_folder_path": str(new_folder),
            "old_series_path": "",
            "old_library_root_id": None,
        },
    )
    db_session.add(action)
    await db_session.flush()

    await service._rollback_action(db_session, _rollback_plan(action))

    assert old_folder.exists()
    assert not new_folder.exists()
    assert (old_folder / "series.json").exists()
    assert series.path is None
    assert series.library_root_id is None
    assert action.status == ImportJobActionStatus.ROLLED_BACK
    assert action.rolled_back_at is not None
