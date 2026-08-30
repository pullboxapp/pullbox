"""Unit tests for immediate library conversion recovery behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

pytest_plugins = ["tests.conftest_security"]

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_convert_library_file_removes_converted_artifact_when_trash_move_fails(
    sec_db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    from pullbox.core.exceptions import ValidationError
    from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot
    from pullbox.services import library_convert_service as service

    source = tmp_path / "Convert Me.cbr"
    converted = tmp_path / "Convert Me.cbz"
    source.write_text("source", encoding="utf-8")

    async def convert_file(_source: Path, _target_format: str) -> Path:
        converted.write_text("converted", encoding="utf-8")
        return converted

    def fail_move(*_args: object, **_kwargs: object) -> Path:
        raise FileExistsError("trash collision")

    monkeypatch.setattr(service, "convert_file", convert_file)
    monkeypatch.setattr(service, "move_file_to_utility_trash", fail_move)

    async with sec_db() as session:
        root = LibraryRoot(name="Comics", path=str(tmp_path), enabled=True)
        session.add(root)
        await session.flush()
        library_file = LibraryFile(
            file_path=str(source),
            file_name=source.name,
            file_size=source.stat().st_size,
            file_format=FileFormat.CBR,
            file_modified_at=datetime.now(tz=UTC),
            library_root_id=root.id,
        )
        session.add(library_file)
        await session.commit()
        library_file_id = library_file.id

        with pytest.raises(ValidationError, match="A CBZ file with that name already exists"):
            await service.convert_library_file(
                session,
                source=source,
                trash_dir=tmp_path / ".trash",
                trash_relative_path=source.name,
            )

        assert source.exists() is True
        assert converted.exists() is False
        refreshed = await session.get(LibraryFile, library_file_id)
        assert refreshed is not None
        assert refreshed.file_path == str(source)
        assert refreshed.file_format == FileFormat.CBR


@pytest.mark.asyncio
async def test_convert_library_file_restores_original_when_later_step_fails(
    sec_db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    from pullbox.core.exceptions import ValidationError
    from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot
    from pullbox.services import library_convert_service as service

    source = tmp_path / "Restore Me.cbr"
    converted = tmp_path / "Restore Me.cbz"
    trash_path = tmp_path / ".trash" / source.name
    source.write_text("source", encoding="utf-8")

    async def convert_file(_source: Path, _target_format: str) -> Path:
        converted.write_text("converted", encoding="utf-8")
        return converted

    def move_to_trash(path: Path, _trash_dir: Path, **_kwargs: object) -> Path:
        trash_path.parent.mkdir(parents=True, exist_ok=True)
        path.replace(trash_path)
        return trash_path

    def restore_from_trash(path: Path, destination: Path, **_kwargs: object) -> None:
        path.replace(destination)
        raise RuntimeError("restore hook failed after moving original")

    async def fail_sync(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("database write failed")

    monkeypatch.setattr(service, "convert_file", convert_file)
    monkeypatch.setattr(service, "move_file_to_utility_trash", move_to_trash)
    monkeypatch.setattr(service, "restore_file_from_utility_trash", restore_from_trash)
    monkeypatch.setattr(service, "_sync_converted_file_record", fail_sync)

    async with sec_db() as session:
        root = LibraryRoot(name="Comics", path=str(tmp_path), enabled=True)
        session.add(root)
        await session.flush()
        library_file = LibraryFile(
            file_path=str(source),
            file_name=source.name,
            file_size=source.stat().st_size,
            file_format=FileFormat.CBR,
            file_modified_at=datetime.now(tz=UTC),
            library_root_id=root.id,
        )
        session.add(library_file)
        await session.commit()

        with pytest.raises(ValidationError, match="Conversion could not be completed"):
            await service.convert_library_file(
                session,
                source=source,
                trash_dir=tmp_path / ".trash",
                trash_relative_path=source.name,
            )

        assert source.exists() is True
        assert trash_path.exists() is False


@pytest.mark.asyncio
async def test_convert_library_file_rejects_referenced_source_before_conversion(
    sec_db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    from pullbox.core.exceptions import ValidationError
    from pullbox.models.library import (
        FileFormat,
        LibraryFile,
        LibraryFileStorageMode,
        LibraryRoot,
    )
    from pullbox.services import library_convert_service as service

    source = tmp_path / "Referenced.cbr"
    original = b"user-owned comic"
    source.write_bytes(original)
    convert = pytest.fail
    monkeypatch.setattr(service, "convert_file", convert)

    async with sec_db() as session:
        root = LibraryRoot(name="Comics", path=str(tmp_path), enabled=True)
        session.add(root)
        await session.flush()
        session.add(
            LibraryFile(
                file_path=str(source),
                file_name=source.name,
                file_size=source.stat().st_size,
                file_format=FileFormat.CBR,
                file_modified_at=datetime.now(tz=UTC),
                library_root_id=root.id,
                storage_mode=LibraryFileStorageMode.REFERENCED,
            )
        )
        await session.commit()

        with pytest.raises(ValidationError, match="Referenced library files cannot be converted"):
            await service.convert_library_file(
                session,
                source=source,
                trash_dir=tmp_path / ".trash",
                trash_relative_path=source.name,
            )

    assert source.read_bytes() == original
