"""Tests for library browser delete service helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from pullbox.core.exceptions import ValidationError
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import (
    FileFormat,
    LibraryFile,
    LibraryFileStorageMode,
    LibraryRoot,
    MatchConfidence,
)
from pullbox.models.series import Series, SeriesStatus
from pullbox.services import library_delete_service
from pullbox.services.library_delete_service import (
    LibraryDeleteContext,
    build_delete_context,
    delete_library_entry,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_root(session: AsyncSession, root_path: Path) -> LibraryRoot:
    root = LibraryRoot(name="Comics", path=str(root_path), enabled=True)
    session.add(root)
    await session.flush()
    return root


async def _seed_series_issue_file(
    session: AsyncSession,
    root: LibraryRoot,
    file_path: Path,
    *,
    monitored: bool = True,
    manual_skip: bool = False,
    series_path: Path | None = None,
    comicvine_id: int | None = None,
    storage_mode: LibraryFileStorageMode = LibraryFileStorageMode.MANAGED,
) -> tuple[Series, Issue, LibraryFile]:
    series = Series(
        title="Library Delete Test",
        sort_title="library delete test",
        year_start=2026,
        status=SeriesStatus.CONTINUING,
        monitored=monitored,
        library_root_id=root.id,
        path=str(series_path or file_path.parent),
        comicvine_id=comicvine_id,
    )
    session.add(series)
    await session.flush()
    issue = Issue(
        series_id=series.id,
        issue_number=1.0,
        status=IssueStatus.OWNED,
        manual_skip=manual_skip,
    )
    session.add(issue)
    await session.flush()
    library_file = LibraryFile(
        issue_id=issue.id,
        library_root_id=root.id,
        file_path=str(file_path),
        file_name=file_path.name,
        file_size=file_path.stat().st_size if file_path.exists() else 10,
        file_format=FileFormat.CBZ,
        file_modified_at=datetime.now(tz=UTC),
        match_confidence=MatchConfidence.HIGH,
        storage_mode=storage_mode,
    )
    session.add(library_file)
    await session.flush()
    return series, issue, library_file


def test_path_helpers_and_permanent_delete(tmp_path: Path) -> None:
    root = tmp_path / "library"
    child = root / "Series"
    child.mkdir(parents=True)
    file_path = child / "issue.cbz"
    file_path.write_bytes(b"comic")

    assert library_delete_service._is_relative_to(child, root) is True
    assert library_delete_service._is_relative_to(root, child) is False
    assert library_delete_service._path_prefix(Path("/tmp/library/")) == "/tmp/library"
    assert library_delete_service._normalize_series_path(None) is None
    assert library_delete_service._normalize_series_path(str(child)) == str(
        child.resolve(strict=False)
    )

    library_delete_service._delete_path_permanently(file_path)
    assert not file_path.exists()

    nested = child / "nested"
    nested.mkdir()
    (nested / "issue.cbz").write_bytes(b"comic")
    library_delete_service._delete_path_permanently(nested)
    assert not nested.exists()

    with pytest.raises(ValidationError, match="no longer exists"):
        library_delete_service._delete_path_permanently(nested)


def test_status_after_library_file_removed_respects_issue_intent() -> None:
    skipped_issue = Issue(series_id=1, issue_number=1.0, manual_skip=True)
    monitored_issue = Issue(
        series=Series(title="Monitored", sort_title="monitored", monitored=True),
        issue_number=1.0,
        manual_skip=False,
    )
    unmonitored_issue = Issue(
        series=Series(title="Unmonitored", sort_title="unmonitored", monitored=False),
        issue_number=1.0,
        manual_skip=False,
    )

    assert library_delete_service._status_after_library_file_removed(skipped_issue) == (
        IssueStatus.SKIPPED
    )
    assert library_delete_service._status_after_library_file_removed(monitored_issue) == (
        IssueStatus.WANTED
    )
    assert library_delete_service._status_after_library_file_removed(unmonitored_issue) == (
        IssueStatus.SKIPPED
    )


def test_trash_relative_path_uses_root_name_for_in_root_paths(tmp_path: Path) -> None:
    root_path = tmp_path / "library"
    source = root_path / "Series" / "issue.cbz"
    root = LibraryRoot(name="Main Library", path=str(root_path))

    assert library_delete_service._trash_relative_path(source, root) == Path(
        "Main Library/Series/issue.cbz"
    )
    assert library_delete_service._trash_relative_path(tmp_path / "outside.cbz", root) == Path(
        "outside.cbz"
    )


@pytest.mark.asyncio
async def test_match_series_folder_record_resolves_exact_normalized_and_cv_suffix(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    exact_folder = tmp_path / "Exact"
    normalized_folder = tmp_path / "Normalized"
    cv_folder = tmp_path / "CV Series [12345]"
    exact_folder.mkdir()
    normalized_folder.mkdir()
    cv_folder.mkdir()
    exact = Series(title="Exact", sort_title="exact", path=str(exact_folder))
    normalized = Series(
        title="Normalized",
        sort_title="normalized",
        path=str(normalized_folder / ".." / "Normalized"),
    )
    cv = Series(
        title="CV Series",
        sort_title="cv series",
        path=str(tmp_path / "CV Series"),
        comicvine_id=12345,
    )
    db_session.add_all([exact, normalized, cv])
    await db_session.flush()

    assert await library_delete_service._match_series_folder_record(db_session, exact_folder) == (
        exact.id,
        "Exact",
    )
    assert await library_delete_service._match_series_folder_record(
        db_session,
        normalized_folder,
    ) == (normalized.id, "Normalized")
    assert await library_delete_service._match_series_folder_record(db_session, cv_folder) == (
        cv.id,
        "CV Series",
    )
    mismatch = Series(
        title="Mismatched CV",
        sort_title="mismatched cv",
        path=str(tmp_path / "Different Name"),
        comicvine_id=22222,
    )
    db_session.add(mismatch)
    await db_session.flush()
    assert (
        await library_delete_service._match_series_folder_record(
            db_session,
            tmp_path / "Mismatched CV [22222]",
        )
        is None
    )
    assert (
        await library_delete_service._match_series_folder_record(
            db_session,
            tmp_path / "No Match [999]",
        )
        is None
    )


@pytest.mark.asyncio
async def test_build_delete_context_describes_root_series_folder_and_file(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "library"
    series_folder = root_path / "Library Delete Test"
    series_folder.mkdir(parents=True)
    file_path = series_folder / "Library Delete Test 001.cbz"
    file_path.write_bytes(b"comic")
    root = await _seed_root(db_session, root_path)
    series, _issue, _library_file = await _seed_series_issue_file(db_session, root, file_path)

    root_context = await build_delete_context(
        db_session,
        target=root_path,
        kind="root",
        trash_enabled=True,
    )
    series_context = await build_delete_context(
        db_session,
        target=series_folder,
        kind="folder",
        trash_enabled=True,
    )
    file_context = await build_delete_context(
        db_session,
        target=file_path,
        kind="file",
        trash_enabled=False,
    )

    assert root_context == LibraryDeleteContext(mode="root", trash_enabled=False)
    assert series_context.mode == "series"
    assert series_context.series_id == series.id
    assert series_context.series_title == "Library Delete Test"
    assert series_context.linked_file_count == 1
    assert file_context.mode == "file"
    assert file_context.tracked_file_count == 1
    assert file_context.has_linked_issue is True
    assert file_context.issue_status_after_delete == IssueStatus.WANTED.value
    assert file_context.issue_status_reason == "series_monitored"


@pytest.mark.asyncio
async def test_build_delete_context_reports_referenced_file_ownership(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "library"
    series_folder = root_path / "Referenced Series"
    series_folder.mkdir(parents=True)
    file_path = series_folder / "Referenced Series 001.cbz"
    file_path.write_bytes(b"user-owned comic")
    root = await _seed_root(db_session, root_path)
    await _seed_series_issue_file(
        db_session,
        root,
        file_path,
        storage_mode=LibraryFileStorageMode.REFERENCED,
    )

    context = await build_delete_context(
        db_session,
        target=file_path,
        kind="file",
        trash_enabled=True,
    )

    assert context.referenced_file_count == 1
    assert context.managed_file_count == 0


@pytest.mark.asyncio
async def test_build_delete_context_reports_manual_skip_and_unmonitored_file_reasons(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "library"
    root_path.mkdir()
    root = await _seed_root(db_session, root_path)
    manual_folder = root_path / "Manual"
    manual_folder.mkdir()
    manual_file = manual_folder / "Manual 001.cbz"
    manual_file.write_bytes(b"comic")
    unmonitored_folder = root_path / "Unmonitored"
    unmonitored_folder.mkdir()
    unmonitored_file = unmonitored_folder / "Unmonitored 001.cbz"
    unmonitored_file.write_bytes(b"comic")
    await _seed_series_issue_file(
        db_session,
        root,
        manual_file,
        manual_skip=True,
    )
    await _seed_series_issue_file(
        db_session,
        root,
        unmonitored_file,
        monitored=False,
    )

    manual_context = await build_delete_context(
        db_session,
        target=manual_file,
        kind="file",
        trash_enabled=True,
    )
    unmonitored_context = await build_delete_context(
        db_session,
        target=unmonitored_file,
        kind="file",
        trash_enabled=True,
    )

    assert manual_context.issue_status_after_delete == IssueStatus.SKIPPED.value
    assert manual_context.issue_status_reason == "manual_skip"
    assert unmonitored_context.issue_status_after_delete == IssueStatus.SKIPPED.value
    assert unmonitored_context.issue_status_reason == "series_unmonitored"


@pytest.mark.asyncio
async def test_build_delete_context_describes_generic_folder_counts(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "library"
    generic_folder = root_path / "Loose Files"
    generic_folder.mkdir(parents=True)
    file_path = generic_folder / "Loose 001.cbz"
    file_path.write_bytes(b"comic")
    root = await _seed_root(db_session, root_path)
    series, _issue, library_file = await _seed_series_issue_file(
        db_session,
        root,
        file_path,
        series_path=root_path / "Other Series",
    )
    series.path = str(generic_folder / "Nested Series")
    library_file.issue_id = None
    await db_session.flush()

    context = await build_delete_context(
        db_session,
        target=generic_folder,
        kind="folder",
        trash_enabled=True,
    )

    assert context.mode == "folder"
    assert context.tracked_file_count == 1
    assert context.tracked_series_count == 1


@pytest.mark.asyncio
async def test_delete_library_entry_rejects_root_and_missing_series_context(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    root = LibraryRoot(name="Library", path=str(tmp_path))

    with pytest.raises(ValidationError, match="Library roots cannot be deleted"):
        await delete_library_entry(
            db_session,
            target=tmp_path,
            root=root,
            kind="root",
            delete_context=LibraryDeleteContext(mode="root", trash_enabled=False),
        )

    with pytest.raises(ValidationError, match="Tracked series information is missing"):
        await delete_library_entry(
            db_session,
            target=tmp_path / "Series",
            root=root,
            kind="folder",
            delete_context=LibraryDeleteContext(mode="series", trash_enabled=True),
        )


@pytest.mark.asyncio
async def test_delete_library_entry_delegates_series_deletion(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = LibraryRoot(name="Library", path=str(tmp_path))
    delete = AsyncMock()
    monkeypatch.setattr(library_delete_service.SeriesService, "delete", delete)

    outcome = await delete_library_entry(
        db_session,
        target=tmp_path / "Series",
        root=root,
        kind="folder",
        delete_context=LibraryDeleteContext(
            mode="series",
            trash_enabled=True,
            series_id=42,
        ),
        trash_dir=tmp_path / ".trash",
    )

    delete.assert_awaited_once_with(
        db_session,
        42,
        delete_files=True,
        delete_folder=True,
        trash_dir=tmp_path / ".trash",
        folder_path_override=tmp_path / "Series",
    )
    assert outcome.mode == "series"
    assert outcome.deleted_via_trash is True


@pytest.mark.asyncio
async def test_delete_library_entry_removes_file_and_updates_issue_status(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "library"
    series_folder = root_path / "Series"
    series_folder.mkdir(parents=True)
    file_path = series_folder / "Series 001.cbz"
    file_path.write_bytes(b"comic")
    root = await _seed_root(db_session, root_path)
    _series, issue, library_file = await _seed_series_issue_file(db_session, root, file_path)

    outcome = await delete_library_entry(
        db_session,
        target=file_path,
        root=root,
        kind="file",
        delete_context=LibraryDeleteContext(mode="file", trash_enabled=False),
    )
    await db_session.flush()

    assert outcome.mode == "file"
    assert outcome.deleted_via_trash is False
    assert not file_path.exists()
    assert issue.status == IssueStatus.WANTED
    assert await db_session.get(LibraryFile, library_file.id) is None


@pytest.mark.asyncio
async def test_delete_library_entry_detaches_referenced_file_without_touching_source(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "library"
    series_folder = root_path / "Referenced Series"
    series_folder.mkdir(parents=True)
    file_path = series_folder / "Referenced Series 001.cbz"
    original = b"user-owned comic"
    file_path.write_bytes(original)
    root = await _seed_root(db_session, root_path)
    _series, issue, library_file = await _seed_series_issue_file(
        db_session,
        root,
        file_path,
        storage_mode=LibraryFileStorageMode.REFERENCED,
    )

    outcome = await delete_library_entry(
        db_session,
        target=file_path,
        root=root,
        kind="file",
        delete_context=LibraryDeleteContext(
            mode="file",
            trash_enabled=True,
            referenced_file_count=1,
        ),
        trash_dir=tmp_path / ".trash",
    )
    await db_session.flush()

    assert file_path.read_bytes() == original
    assert issue.status == IssueStatus.WANTED
    assert await db_session.get(LibraryFile, library_file.id) is None
    assert outcome.deleted_via_trash is False
    assert outcome.referenced_files_detached == 1
    assert outcome.managed_files_deleted == 0


@pytest.mark.asyncio
async def test_delete_library_entry_preserves_referenced_file_in_mixed_folder(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "library"
    folder = root_path / "Mixed"
    folder.mkdir(parents=True)
    referenced_path = folder / "Referenced 001.cbz"
    managed_path = folder / "Managed 001.cbz"
    referenced_path.write_bytes(b"reference")
    managed_path.write_bytes(b"managed")
    root = await _seed_root(db_session, root_path)
    _series, _issue, referenced_file = await _seed_series_issue_file(
        db_session,
        root,
        referenced_path,
        series_path=root_path / "Other Referenced",
        storage_mode=LibraryFileStorageMode.REFERENCED,
    )
    _managed_series, _managed_issue, managed_file = await _seed_series_issue_file(
        db_session,
        root,
        managed_path,
        series_path=root_path / "Other Managed",
    )

    outcome = await delete_library_entry(
        db_session,
        target=folder,
        root=root,
        kind="folder",
        delete_context=LibraryDeleteContext(
            mode="folder",
            trash_enabled=False,
            referenced_file_count=1,
            managed_file_count=1,
        ),
    )
    await db_session.flush()

    assert folder.is_dir()
    assert referenced_path.read_bytes() == b"reference"
    assert not managed_path.exists()
    assert await db_session.get(LibraryFile, referenced_file.id) is None
    assert await db_session.get(LibraryFile, managed_file.id) is None
    assert outcome.referenced_files_detached == 1
    assert outcome.managed_files_deleted == 1


@pytest.mark.asyncio
async def test_delete_library_entry_trashes_folder_and_clears_nested_series_paths(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path = tmp_path / "library"
    folder = root_path / "Loose"
    folder.mkdir(parents=True)
    file_path = folder / "Loose 001.cbz"
    file_path.write_bytes(b"comic")
    trash_dir = tmp_path / ".trash"
    root = await _seed_root(db_session, root_path)
    series, _issue, library_file = await _seed_series_issue_file(
        db_session,
        root,
        file_path,
        monitored=False,
        series_path=folder / "Nested",
    )
    library_file.issue_id = None
    await db_session.flush()

    def fake_move_path_to_utility_trash(
        source: Path,
        trash_root: Path,
        *,
        relative_path: Path,
    ) -> Path:
        assert source == folder
        assert trash_root == trash_dir
        return trash_root / relative_path

    monkeypatch.setattr(
        library_delete_service,
        "move_path_to_utility_trash",
        fake_move_path_to_utility_trash,
    )

    outcome = await delete_library_entry(
        db_session,
        target=folder,
        root=root,
        kind="folder",
        delete_context=LibraryDeleteContext(mode="folder", trash_enabled=True),
        trash_dir=trash_dir,
    )
    await db_session.flush()

    assert outcome.deleted_via_trash is True
    assert outcome.result_path == str(trash_dir / "Comics" / "Loose")
    assert series.path is None
    assert await db_session.get(LibraryFile, library_file.id) is None


@pytest.mark.asyncio
async def test_delete_library_entry_wraps_trash_name_collisions(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = LibraryRoot(name="Library", path=str(tmp_path))

    def fake_move_path_to_utility_trash(
        source: Path,
        trash_root: Path,
        *,
        relative_path: Path,
    ) -> Path:
        _ = source, trash_root, relative_path
        raise FileExistsError("trash already has that file")

    monkeypatch.setattr(
        library_delete_service,
        "move_path_to_utility_trash",
        fake_move_path_to_utility_trash,
    )

    with pytest.raises(ValidationError, match="trash already has that file"):
        await delete_library_entry(
            db_session,
            target=tmp_path / "file.cbz",
            root=root,
            kind="file",
            delete_context=LibraryDeleteContext(mode="file", trash_enabled=True),
            trash_dir=tmp_path / ".trash",
        )
