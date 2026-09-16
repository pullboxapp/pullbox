"""Tests for mass-convert preview builder helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from pullbox.core.exceptions import ValidationError
from pullbox.utilities.preview_builders import build_mass_convert_preview
from pullbox.utilities.schemas import MassConvertPreviewRequest

if TYPE_CHECKING:
    from pathlib import Path


class _SessionResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def scalars(self) -> _SessionResult:
        return self


class _FakeSession:
    def __init__(self, root_path: Path) -> None:
        self.root_path = root_path
        self._execute_count = 0

    async def execute(self, _stmt: Any) -> _SessionResult:
        self._execute_count += 1
        if self._execute_count == 1:
            return _SessionResult([(str(self.root_path),)])
        return _SessionResult([])


@pytest.mark.asyncio
async def test_mass_convert_preview_folder_scope_excludes_trash_folder(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    series = library / "Batman"
    trash = library / ".trash"
    series.mkdir(parents=True)
    trash.mkdir()
    keep = series / "Batman 001.cbr"
    skip = trash / "Batman 002.cbr"
    keep.write_bytes(b"keep")
    skip.write_bytes(b"skip")

    preview = await build_mass_convert_preview(
        MassConvertPreviewRequest(
            scope="folder",
            file_paths=[str(library)],
            trash_folder=str(trash),
        ),
        session=_FakeSession(library),
        load_trash_context=None,
    )

    assert preview.scope == "folder"
    assert preview.item_count == 1
    assert preview.total_size_bytes == keep.stat().st_size
    assert preview.items[0].file_path == str(keep)
    assert preview.items[0].source_format == "CBR"
    assert preview.items[0].output_name == "Batman 001.cbz"


@pytest.mark.asyncio
async def test_mass_convert_preview_manual_scope_infers_supported_formats(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    pdf = library / "Annual.pdf"
    cb7 = library / "Special.cb7"
    ignored = library / "notes.txt"
    pdf.write_bytes(b"pdf")
    cb7.write_bytes(b"cb7")
    ignored.write_bytes(b"text")

    preview = await build_mass_convert_preview(
        MassConvertPreviewRequest(
            scope="manual",
            file_paths=[str(pdf), str(cb7), str(ignored)],
            trash_folder=str(tmp_path / ".trash"),
        ),
        session=_FakeSession(library),
        load_trash_context=None,
    )

    assert preview.item_count == 2
    assert [(item.source_name, item.source_format) for item in preview.items] == [
        ("Annual.pdf", "PDF"),
        ("Special.cb7", "CB7"),
    ]


@pytest.mark.asyncio
async def test_mass_convert_preview_rejects_paths_outside_library_roots(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    outside = tmp_path / "outside"
    library.mkdir()
    outside.mkdir()
    pdf = outside / "Annual.pdf"
    pdf.write_bytes(b"pdf")

    with pytest.raises(ValidationError, match="outside"):
        await build_mass_convert_preview(
            MassConvertPreviewRequest(
                scope="manual",
                file_paths=[str(pdf)],
                trash_folder=str(tmp_path / ".trash"),
            ),
            session=_FakeSession(library),
            load_trash_context=None,
        )
