"""Tests for library permissions preview builder helpers."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pytest

from pullbox.utilities.preview_builders import build_library_permissions_preview
from pullbox.utilities.schemas import LibraryPermissionsPreviewRequest

if TYPE_CHECKING:
    from pathlib import Path


class _SessionResult:
    def __init__(self, rows: list[tuple[int, str]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[int, str]]:
        return self._rows


class _FakeSession:
    def __init__(self, root_path: Path) -> None:
        self.root_path = root_path

    async def execute(self, _stmt: Any) -> _SessionResult:
        return _SessionResult([(1, str(self.root_path))])


@pytest.mark.asyncio
async def test_library_permissions_preview_counts_folder_scope(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    folder = root / "Batman"
    nested = folder / "Annuals"
    nested.mkdir(parents=True)
    (folder / "Batman 001.cbz").write_bytes(b"one")
    (nested / "Batman Annual.cbz").write_bytes(b"two")

    preview = await build_library_permissions_preview(
        LibraryPermissionsPreviewRequest(
            scope="folder",
            file_paths=[str(folder)],
            folder_mode="750",
            file_mode="640",
            include_folders=True,
            include_files=True,
        ),
        session=_FakeSession(root),
    )

    assert preview.scope == "folder"
    assert preview.folder_count == 2
    assert preview.file_count == 2
    assert preview.item_count == 4
    assert any(item.name == "Batman" and item.item_type == "folder" for item in preview.items)
    assert any(
        item.name == "Batman 001.cbz" and item.target_mode == "640" for item in preview.items
    )


@pytest.mark.asyncio
async def test_library_permissions_preview_respects_file_only_scope(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    comic = root / "Batman 001.cbz"
    comic.write_bytes(b"one")

    preview = await build_library_permissions_preview(
        LibraryPermissionsPreviewRequest(
            scope="files",
            file_paths=[str(comic)],
            folder_mode="750",
            file_mode="640",
            include_folders=False,
            include_files=True,
        ),
        session=_FakeSession(root),
    )

    assert preview.scope == "files"
    assert preview.folder_count == 0
    assert preview.file_count == 1
    assert preview.items[0].name == "Batman 001.cbz"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support unavailable")
@pytest.mark.asyncio
async def test_library_permissions_preview_preserves_external_symlink_item(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    folder = root / "Batman"
    external_target = tmp_path / "outside" / "Archive"
    external_target.mkdir(parents=True)
    folder.mkdir(parents=True)
    link = folder / "External Archive"
    link.symlink_to(external_target, target_is_directory=True)

    preview = await build_library_permissions_preview(
        LibraryPermissionsPreviewRequest(
            scope="folder",
            file_paths=[str(folder)],
            folder_mode="750",
            file_mode="640",
            include_folders=True,
            include_files=True,
        ),
        session=_FakeSession(root),
    )

    assert any(
        item.name == "External Archive" and item.item_type == "symlink" for item in preview.items
    )


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support unavailable")
@pytest.mark.asyncio
async def test_library_permissions_preview_accepts_symlinked_library_root(
    tmp_path: Path,
) -> None:
    real_root = tmp_path / "real-library"
    linked_root = tmp_path / "linked-library"
    folder = real_root / "Batman"
    folder.mkdir(parents=True)
    (folder / "Batman 001.cbz").write_bytes(b"one")
    linked_root.symlink_to(real_root, target_is_directory=True)

    preview = await build_library_permissions_preview(
        LibraryPermissionsPreviewRequest(
            scope="folder",
            file_paths=[str(linked_root / "Batman")],
            folder_mode="750",
            file_mode="640",
            include_folders=True,
            include_files=True,
        ),
        session=_FakeSession(linked_root),
    )

    assert preview.scope == "folder"
    assert preview.folder_count == 1
    assert preview.file_count == 1
    assert {item.name for item in preview.items} == {"Batman", "Batman 001.cbz"}
