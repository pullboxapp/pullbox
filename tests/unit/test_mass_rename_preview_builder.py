"""Tests for mass rename preview builder validation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from pullbox.core.exceptions import ValidationError
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import (
    FileFormat,
    LibraryFile,
    LibraryFileStorageMode,
    LibraryRoot,
    MatchConfidence,
)
from pullbox.models.publisher import Publisher
from pullbox.models.series import Series, SeriesStatus
from pullbox.utilities import preview_builders
from pullbox.utilities.preview_builders import build_mass_rename_preview
from pullbox.utilities.schemas import MassRenamePreviewRequest

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_mass_rename_preview_rejects_invalid_target_before_db_work() -> None:
    with pytest.raises(ValidationError, match="target must be 'files' or 'folders'"):
        await build_mass_rename_preview(
            MassRenamePreviewRequest(target="covers", scope="library"),
            session=None,
        )


@pytest.mark.asyncio
async def test_mass_rename_preview_requires_manual_selection_before_db_work() -> None:
    with pytest.raises(ValidationError, match="Choose at least one file or folder"):
        await build_mass_rename_preview(
            MassRenamePreviewRequest(target="files", scope="manual", file_paths=[]),
            session=None,
        )


async def _create_library_file_fixture(
    session: AsyncSession,
    root_path: Path,
    file_path: Path,
    *,
    issue_number: float = 1.0,
    storage_mode: LibraryFileStorageMode = LibraryFileStorageMode.MANAGED,
) -> None:
    root = (
        await session.execute(select(LibraryRoot).where(LibraryRoot.path == str(root_path)))
    ).scalar_one_or_none()
    if root is None:
        root = LibraryRoot(name="Library", path=str(root_path), enabled=True)
        session.add(root)
    series = (
        await session.execute(select(Series).where(Series.path == str(file_path.parent)))
    ).scalar_one_or_none()
    if series is None:
        publisher = Publisher(name=f"DC Comics {issue_number:g}")
        series = Series(
            title="Batman",
            sort_title="Batman",
            year_start=2026,
            status=SeriesStatus.CONTINUING,
            monitored=True,
            publisher=publisher,
            library_root=root,
            path=str(file_path.parent),
        )
    issue = Issue(
        series=series,
        issue_number=issue_number,
        title=f"Issue {issue_number:g}",
        issue_type=IssueType.ISSUE,
        status=IssueStatus.OWNED,
    )
    library_file = LibraryFile(
        issue=issue,
        library_root=root,
        file_path=str(file_path),
        file_name=file_path.name,
        file_size=file_path.stat().st_size,
        file_format=FileFormat.CBZ,
        file_modified_at=datetime.now(tz=UTC),
        match_confidence=MatchConfidence.HIGH,
        storage_mode=storage_mode,
    )
    session.add(library_file)
    await session.flush()


async def _create_series_fixture(
    session: AsyncSession,
    root_path: Path,
    folder_path: Path,
) -> None:
    publisher = Publisher(name="DC Comics")
    root = LibraryRoot(name="Library", path=str(root_path), enabled=True)
    series = Series(
        title="Batman",
        sort_title="Batman",
        year_start=2026,
        status=SeriesStatus.CONTINUING,
        monitored=True,
        publisher=publisher,
        library_root=root,
        path=str(folder_path),
    )
    session.add(series)
    await session.flush()


async def _stub_naming_config(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def fake_load_naming_config(_session: object) -> dict[str, str]:
        return {}

    monkeypatch.setattr(preview_builders, "_load_naming_config", fake_load_naming_config)


@pytest.mark.asyncio
async def test_mass_rename_preview_manual_file_without_metadata_is_unmatched(
    monkeypatch,  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    await _stub_naming_config(monkeypatch)
    root_path = tmp_path / "library"
    root_path.mkdir()
    selected_file = root_path / "orphan.cbz"
    selected_file.write_bytes(b"orphan")
    db_session.add(LibraryRoot(name="Library", path=str(root_path), enabled=True))
    await db_session.flush()

    preview = await build_mass_rename_preview(
        MassRenamePreviewRequest(
            target="files",
            scope="manual",
            file_paths=[str(selected_file)],
        ),
        session=db_session,
    )

    assert preview.item_count == 1
    assert preview.actionable_count == 0
    assert preview.items[0].status == "unmatched"
    assert preview.items[0].reason == "No linked library metadata found for this file."


@pytest.mark.asyncio
async def test_mass_rename_preview_manual_file_reports_ready_and_conflict_statuses(
    monkeypatch,  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    await _stub_naming_config(monkeypatch)
    root_path = tmp_path / "library"
    series_folder = root_path / "Batman"
    series_folder.mkdir(parents=True)
    source_file = series_folder / "Wrong Name.cbz"
    source_file.write_bytes(b"issue")
    await _create_library_file_fixture(db_session, root_path, source_file)
    monkeypatch.setattr(
        preview_builders,
        "compute_target_filename",
        lambda *_args, **_kwargs: "Batman 001.cbz",
    )

    ready_preview = await build_mass_rename_preview(
        MassRenamePreviewRequest(
            target="files",
            scope="manual",
            file_paths=[str(source_file)],
        ),
        session=db_session,
    )

    assert ready_preview.actionable_count == 1
    assert ready_preview.items[0].status == "ready"
    assert ready_preview.items[0].proposed_name == "Batman 001.cbz"
    assert ready_preview.items[0].template_key == "comic_file_template"

    (series_folder / "Batman 001.cbz").write_bytes(b"existing")
    conflict_preview = await build_mass_rename_preview(
        MassRenamePreviewRequest(
            target="files",
            scope="manual",
            file_paths=[str(source_file)],
        ),
        session=db_session,
    )

    assert conflict_preview.actionable_count == 1
    assert conflict_preview.items[0].status == "conflict"
    assert conflict_preview.items[0].reason == "Target already exists: Batman 001.cbz"


@pytest.mark.asyncio
async def test_mass_rename_preview_blocks_referenced_file_and_parent_folder(
    monkeypatch,  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    await _stub_naming_config(monkeypatch)
    root_path = tmp_path / "library"
    folder = root_path / "Batman"
    folder.mkdir(parents=True)
    source_file = folder / "Wrong Name.cbz"
    source_file.write_bytes(b"reference")
    await _create_library_file_fixture(
        db_session,
        root_path,
        source_file,
        storage_mode=LibraryFileStorageMode.REFERENCED,
    )

    file_preview = await build_mass_rename_preview(
        MassRenamePreviewRequest(
            target="files",
            scope="manual",
            file_paths=[str(source_file)],
        ),
        session=db_session,
    )
    folder_preview = await build_mass_rename_preview(
        MassRenamePreviewRequest(
            target="folders",
            scope="manual",
            file_paths=[str(folder)],
        ),
        session=db_session,
    )

    assert file_preview.actionable_count == 0
    assert file_preview.items[0].status == "blocked"
    assert file_preview.items[0].reason == "Referenced library files stay unchanged on disk."
    assert folder_preview.actionable_count == 0
    assert folder_preview.items[0].status == "blocked"
    assert "contains referenced files" in (folder_preview.items[0].reason or "")


@pytest.mark.asyncio
async def test_mass_rename_preview_marks_duplicate_file_targets_as_conflicts(
    monkeypatch,  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    await _stub_naming_config(monkeypatch)
    root_path = tmp_path / "library"
    series_folder = root_path / "Batman"
    series_folder.mkdir(parents=True)
    first_file = series_folder / "First.cbz"
    second_file = series_folder / "Second.cbz"
    first_file.write_bytes(b"first")
    second_file.write_bytes(b"second")
    await _create_library_file_fixture(db_session, root_path, first_file, issue_number=1.0)
    await _create_library_file_fixture(db_session, root_path, second_file, issue_number=2.0)
    monkeypatch.setattr(
        preview_builders,
        "compute_target_filename",
        lambda *_args, **_kwargs: "Duplicate.cbz",
    )

    preview = await build_mass_rename_preview(
        MassRenamePreviewRequest(
            target="files",
            scope="manual",
            file_paths=[str(first_file), str(second_file)],
        ),
        session=db_session,
    )

    assert preview.actionable_count == 0
    assert [item.status for item in preview.items] == ["conflict", "conflict"]
    assert {item.reason for item in preview.items} == {
        "Another selected item resolves to the same target name."
    }


@pytest.mark.asyncio
async def test_mass_rename_preview_manual_folder_reports_ready_conflict_and_unmatched(
    monkeypatch,  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    await _stub_naming_config(monkeypatch)
    root_path = tmp_path / "library"
    folder = root_path / "Wrong Folder"
    unmatched_folder = root_path / "No Metadata"
    folder.mkdir(parents=True)
    unmatched_folder.mkdir()
    await _create_series_fixture(db_session, root_path, folder)
    monkeypatch.setattr(
        preview_builders,
        "build_series_folder_name",
        lambda *_args, **_kwargs: "Batman (2026)",
    )

    ready_preview = await build_mass_rename_preview(
        MassRenamePreviewRequest(
            target="folders",
            scope="manual",
            file_paths=[str(folder), str(unmatched_folder)],
        ),
        session=db_session,
    )

    assert ready_preview.item_count == 2
    assert ready_preview.actionable_count == 1
    assert [(item.status, item.proposed_name) for item in ready_preview.items] == [
        ("ready", "Batman (2026)"),
        ("unmatched", "No Metadata"),
    ]

    (root_path / "Batman (2026)").mkdir()
    conflict_preview = await build_mass_rename_preview(
        MassRenamePreviewRequest(
            target="folders",
            scope="manual",
            file_paths=[str(folder)],
        ),
        session=db_session,
    )

    assert conflict_preview.actionable_count == 1
    assert conflict_preview.items[0].status == "conflict"
    assert conflict_preview.items[0].reason == "Target already exists: Batman (2026)"
