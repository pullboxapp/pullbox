"""Tests for converter preview builder helpers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from pullbox.core.exceptions import ValidationError
from pullbox.utilities.preview_builders import (
    build_convert_preview_response,
    build_library_permissions_preview,
    build_mass_convert_preview,
)
from pullbox.utilities.schemas import (
    ConvertPreviewRequest,
    LibraryPermissionsPreviewRequest,
    MassConvertPreviewRequest,
)

if TYPE_CHECKING:
    from pathlib import Path


class _FakeScalarResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return self._rows


class _FakeResult:
    def __init__(
        self,
        *,
        rows: list[tuple[object, ...]] | None = None,
        scalars: list[object] | None = None,
    ) -> None:
        self._rows = rows or []
        self._scalars = scalars or []

    def all(self) -> list[tuple[object, ...]]:
        return self._rows

    def scalars(self) -> _FakeScalarResult:
        return _FakeScalarResult(self._scalars)


class _FakeSession:
    def __init__(self, *results: _FakeResult) -> None:
        self._results = list(results)

    async def execute(self, _statement: object) -> _FakeResult:
        return self._results.pop(0)


def test_convert_preview_builder_delegates_manual_file_preview(tmp_path: Path) -> None:
    first = tmp_path / "Batman 001.cbr"
    second = tmp_path / "Batman 002.cbr"
    first.write_bytes(b"one")
    second.write_bytes(b"two")

    preview = build_convert_preview_response(
        ConvertPreviewRequest(
            source_format="cbr",
            target_format="cbz",
            scope="manual",
            file_paths=[str(first), str(second)],
        )
    )

    assert preview.source_format == "cbr"
    assert preview.target_format == "cbz"
    assert preview.lossless is True
    assert preview.total_count == 2
    assert preview.total_size_bytes == first.stat().st_size + second.stat().st_size
    assert [file.output_path for file in preview.files] == [
        str(first.with_suffix(".cbz")),
        str(second.with_suffix(".cbz")),
    ]


def test_convert_preview_builder_wraps_executor_validation_errors(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from pullbox.utilities.executors import file_converter

    def raise_validation_error(**_kwargs: object) -> None:
        raise ValueError("unsupported conversion")

    monkeypatch.setattr(file_converter, "build_convert_preview", raise_validation_error)

    with pytest.raises(ValidationError, match="unsupported conversion"):
        build_convert_preview_response(
            ConvertPreviewRequest(source_format="cbr", target_format="cbz", scope="library")
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "message"),
    [
        (MassConvertPreviewRequest(scope="outside"), "scope must be"),
        (MassConvertPreviewRequest(scope="manual"), "Choose at least one file"),
        (MassConvertPreviewRequest(scope="folder"), "Choose at least one folder"),
        (
            MassConvertPreviewRequest(scope="library"),
            "trash_folder is required",
        ),
    ],
)
async def test_mass_convert_preview_rejects_invalid_requests(
    body: MassConvertPreviewRequest,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        await build_mass_convert_preview(body, session=None, load_trash_context=None)


@pytest.mark.asyncio
async def test_mass_convert_preview_manual_scope_filters_and_dedupes_candidates(
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "library"
    trash_dir = library_root / ".trash"
    library_root.mkdir()
    trash_dir.mkdir()
    cbr_file = library_root / "Batman 001.cbr"
    cb7_file = library_root / "Batman 002.cb7"
    pdf_file = library_root / "Batman 003.pdf"
    unsupported_file = library_root / "notes.txt"
    trashed_file = trash_dir / "Batman 004.cbr"
    for file_path in (cbr_file, cb7_file, pdf_file, unsupported_file, trashed_file):
        file_path.write_bytes(file_path.name.encode())
    session = _FakeSession(
        _FakeResult(rows=[(str(library_root),)]),
        _FakeResult(scalars=[]),
    )

    preview = await build_mass_convert_preview(
        MassConvertPreviewRequest(
            scope="manual",
            file_paths=[
                str(cbr_file),
                str(cb7_file),
                str(pdf_file),
                str(cbr_file),
                str(unsupported_file),
                str(trashed_file),
            ],
            trash_folder=str(trash_dir),
        ),
        session=session,
        load_trash_context=None,
    )

    assert preview.scope == "manual"
    assert preview.item_count == 3
    assert preview.total_size_bytes == (
        cbr_file.stat().st_size + cb7_file.stat().st_size + pdf_file.stat().st_size
    )
    assert [(item.source_name, item.source_format, item.output_name) for item in preview.items] == [
        ("Batman 001.cbr", "CBR", "Batman 001.cbz"),
        ("Batman 002.cb7", "CB7", "Batman 002.cbz"),
        ("Batman 003.pdf", "PDF", "Batman 003.cbz"),
    ]


@pytest.mark.asyncio
async def test_mass_convert_preview_folder_scope_scans_supported_files(
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "library"
    folder = library_root / "Batman"
    trash_dir = library_root / ".trash"
    folder.mkdir(parents=True)
    trash_dir.mkdir()
    cbr_file = folder / "Batman 001.cbr"
    cbz_file = folder / "Batman 002.cbz"
    ignored_file = folder / "notes.txt"
    trashed_file = trash_dir / "Batman 003.cbr"
    for file_path in (cbr_file, cbz_file, ignored_file, trashed_file):
        file_path.write_bytes(file_path.name.encode())
    session = _FakeSession(
        _FakeResult(rows=[(str(library_root),)]),
        _FakeResult(scalars=[]),
    )

    preview = await build_mass_convert_preview(
        MassConvertPreviewRequest(
            scope="folder",
            file_paths=[str(folder)],
            trash_folder=str(trash_dir),
        ),
        session=session,
        load_trash_context=None,
    )

    assert preview.scope == "folder"
    assert preview.item_count == 2
    assert [item.source_name for item in preview.items] == ["Batman 001.cbr", "Batman 002.cbz"]
    assert [item.source_format for item in preview.items] == ["CBR", "CBZ"]


@pytest.mark.asyncio
async def test_mass_convert_preview_library_scope_uses_trash_context_and_existing_files(
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "library"
    trash_dir = library_root / ".trash"
    library_root.mkdir()
    trash_dir.mkdir()
    kept_file = library_root / "Wonder Woman 001.cbr"
    missing_file = library_root / "Missing.cbr"
    trashed_file = trash_dir / "Trashed.cbr"
    directory_path = library_root / "Folder.cbr"
    kept_file.write_bytes(b"kept")
    trashed_file.write_bytes(b"trashed")
    directory_path.mkdir()

    async def load_trash_context(_session: object) -> tuple[Path, int]:
        return trash_dir, 123

    session = _FakeSession(
        _FakeResult(
            scalars=[
                SimpleNamespace(file_path=str(kept_file)),
                SimpleNamespace(file_path=str(missing_file)),
                SimpleNamespace(file_path=str(trashed_file)),
                SimpleNamespace(file_path=str(directory_path)),
            ]
        )
    )

    preview = await build_mass_convert_preview(
        MassConvertPreviewRequest(scope="library"),
        session=session,
        load_trash_context=load_trash_context,
    )

    assert preview.scope == "library"
    assert preview.item_count == 1
    assert preview.total_size_bytes == kept_file.stat().st_size
    assert preview.items[0].source_name == kept_file.name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "message"),
    [
        (LibraryPermissionsPreviewRequest(scope="outside"), "scope must be"),
        (LibraryPermissionsPreviewRequest(scope="folder"), "Choose at least one folder"),
        (LibraryPermissionsPreviewRequest(scope="files"), "Choose at least one file"),
        (
            LibraryPermissionsPreviewRequest(
                scope="files",
                file_paths=["/tmp/example.cbz"],
                include_files=False,
            ),
            "include_files must be true",
        ),
    ],
)
async def test_library_permissions_preview_rejects_invalid_requests(
    body: LibraryPermissionsPreviewRequest,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        await build_library_permissions_preview(body, session=_FakeSession())


@pytest.mark.asyncio
async def test_library_permissions_preview_surfaces_executor_config_errors(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    from pullbox.utilities.executors import library_permissions

    class FakeExecutor:
        def validate_config(self, _config: dict[str, object]) -> list[str]:
            return ["bad permissions config"]

    monkeypatch.setattr(library_permissions, "LibraryPermissionsExecutor", FakeExecutor)
    session = _FakeSession(
        _FakeResult(rows=[(1, str(tmp_path))]),
        _FakeResult(scalars=[]),
    )

    with pytest.raises(ValidationError, match="bad permissions config"):
        await build_library_permissions_preview(
            LibraryPermissionsPreviewRequest(scope="library"),
            session=session,
        )


@pytest.mark.asyncio
async def test_library_permissions_preview_wraps_executor_generation_errors(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    from pullbox.utilities.executors import library_permissions

    class FakeExecutor:
        def validate_config(self, _config: dict[str, object]) -> list[str]:
            return []

        async def generate_items(
            self,
            _config: dict[str, object],
            _context: dict[str, object],
        ) -> list[dict[str, str]]:
            raise ValueError("selected path is unsafe")

    monkeypatch.setattr(library_permissions, "LibraryPermissionsExecutor", FakeExecutor)
    session = _FakeSession(
        _FakeResult(rows=[(1, str(tmp_path))]),
        _FakeResult(scalars=[]),
    )

    with pytest.raises(ValidationError, match="selected path is unsafe"):
        await build_library_permissions_preview(
            LibraryPermissionsPreviewRequest(scope="library"),
            session=session,
        )


@pytest.mark.asyncio
async def test_library_permissions_preview_counts_generated_file_folder_and_symlink_items(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    from pullbox.utilities.executors import library_permissions

    root = tmp_path / "library"
    folder = root / "Series"
    file_path = folder / "Issue 001.cbz"
    symlink_path = root / "linked.cbz"
    folder.mkdir(parents=True)
    file_path.write_bytes(b"issue")
    symlink_path.symlink_to(file_path)

    class FakeExecutor:
        def validate_config(self, config: dict[str, object]) -> list[str]:
            assert config["scope"] == "library"
            assert config["run_mode"] == "dry_run"
            return []

        async def generate_items(
            self,
            _config: dict[str, object],
            context: dict[str, object],
        ) -> list[dict[str, str]]:
            assert context == {
                "library_roots": [{"id": 1, "path": str(root)}],
                "referenced_paths": [],
            }
            return [
                {"file_path": str(folder)},
                {"file_path": str(file_path)},
                {"file_path": str(symlink_path)},
            ]

    monkeypatch.setattr(library_permissions, "LibraryPermissionsExecutor", FakeExecutor)
    session = _FakeSession(
        _FakeResult(rows=[(1, str(root))]),
        _FakeResult(scalars=[]),
    )

    preview = await build_library_permissions_preview(
        LibraryPermissionsPreviewRequest(
            scope="library",
            folder_mode="750",
            file_mode="640",
        ),
        session=session,
    )

    assert preview.scope == "library"
    assert preview.item_count == 3
    assert preview.folder_count == 1
    assert preview.file_count == 2
    assert [(item.name, item.item_type, item.target_mode) for item in preview.items] == [
        ("Series", "folder", "750"),
        ("Issue 001.cbz", "file", "640"),
        ("linked.cbz", "symlink", "640"),
    ]


@pytest.mark.asyncio
async def test_library_permissions_preview_rejects_generated_paths_outside_roots(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    from pullbox.utilities.executors import library_permissions

    root = tmp_path / "library"
    outside_file = tmp_path / "outside.cbz"
    root.mkdir()
    outside_file.write_bytes(b"outside")

    class FakeExecutor:
        def validate_config(self, _config: dict[str, object]) -> list[str]:
            return []

        async def generate_items(
            self,
            _config: dict[str, object],
            _context: dict[str, object],
        ) -> list[dict[str, str]]:
            return [{"file_path": str(outside_file)}]

    monkeypatch.setattr(library_permissions, "LibraryPermissionsExecutor", FakeExecutor)
    session = _FakeSession(
        _FakeResult(rows=[(1, str(root))]),
        _FakeResult(scalars=[]),
    )

    with pytest.raises(ValidationError, match="outside enabled library roots"):
        await build_library_permissions_preview(
            LibraryPermissionsPreviewRequest(scope="library"),
            session=session,
        )
