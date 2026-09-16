"""Mutation utilities must skip user-owned referenced library paths."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING

from pullbox.utilities.base_executor import ItemResult
from pullbox.utilities.executors.file_converter import FileConverterExecutor
from pullbox.utilities.executors.library_permissions import LibraryPermissionsExecutor
from pullbox.utilities.executors.mass_convert_pipeline import MassConvertPipelineExecutor
from pullbox.utilities.executors.mass_rename import MassRenameExecutor

if TYPE_CHECKING:
    from pathlib import Path


def test_mass_rename_skips_referenced_file_without_renaming(tmp_path: Path) -> None:
    source = tmp_path / "Existing.cbz"
    source.write_bytes(b"user-owned comic")

    processed = MassRenameExecutor().process_item(
        {
            "id": "rename-1",
            "file_path": str(source),
            "proposed_name": "Renamed.cbz",
        },
        {"target": "files", "template": "{Series}"},
        {"referenced_paths": [str(source)]},
    )

    assert processed.result == ItemResult.SKIPPED
    assert processed.after_state["reason"] == "referenced_file"
    assert source.read_bytes() == b"user-owned comic"
    assert not (tmp_path / "Renamed.cbz").exists()


def test_mass_rename_skips_folder_containing_referenced_file(tmp_path: Path) -> None:
    folder = tmp_path / "Existing"
    folder.mkdir()
    source = folder / "Issue.cbz"
    source.write_bytes(b"user-owned comic")

    processed = MassRenameExecutor().process_item(
        {
            "id": "rename-2",
            "file_path": str(folder),
            "proposed_name": "Renamed",
        },
        {"target": "folders", "template": "{Series}"},
        {"referenced_paths": [str(source)]},
    )

    assert processed.result == ItemResult.SKIPPED
    assert folder.is_dir()
    assert source.read_bytes() == b"user-owned comic"


def test_mass_convert_skips_referenced_file_without_creating_output(tmp_path: Path) -> None:
    source = tmp_path / "Existing.cbr"
    source.write_bytes(b"user-owned comic")

    processed = MassConvertPipelineExecutor().process_item(
        {"id": "convert-1", "file_path": str(source)},
        {"steps": [1]},
        {"referenced_paths": [str(source)]},
    )

    assert processed.result == ItemResult.SKIPPED
    assert processed.after_state["reason"] == "referenced_file"
    assert source.read_bytes() == b"user-owned comic"
    assert not source.with_suffix(".cbz").exists()


def test_file_converter_skips_referenced_file_without_creating_output(tmp_path: Path) -> None:
    source = tmp_path / "Existing.cbr"
    source.write_bytes(b"user-owned comic")

    processed = FileConverterExecutor().process_item(
        {"id": "convert-2", "file_path": str(source)},
        {"target_format": "cbz"},
        {"referenced_paths": [str(source)]},
    )

    assert processed.result == ItemResult.SKIPPED
    assert processed.after_state["reason"] == "referenced_file"
    assert source.read_bytes() == b"user-owned comic"
    assert not source.with_suffix(".cbz").exists()


def test_permissions_skip_referenced_file_and_ancestor_folder(tmp_path: Path) -> None:
    folder = tmp_path / "Existing"
    folder.mkdir(mode=0o750)
    source = folder / "Issue.cbz"
    source.write_bytes(b"user-owned comic")
    source.chmod(0o640)
    context = {"referenced_paths": [str(source)]}
    config = {
        "scope": "paths",
        "run_mode": "apply",
        "confirm_apply": True,
        "file_mode": "600",
        "folder_mode": "700",
    }
    before_file_mode = stat.S_IMODE(source.stat().st_mode)
    before_folder_mode = stat.S_IMODE(folder.stat().st_mode)

    file_result = LibraryPermissionsExecutor().process_item(
        {"id": "permission-1", "file_path": str(source)},
        config,
        context,
    )
    folder_result = LibraryPermissionsExecutor().process_item(
        {"id": "permission-2", "file_path": str(folder)},
        config,
        context,
    )

    assert file_result.result == ItemResult.SKIPPED
    assert folder_result.result == ItemResult.SKIPPED
    assert stat.S_IMODE(source.stat().st_mode) == before_file_mode
    assert stat.S_IMODE(folder.stat().st_mode) == before_folder_mode
