"""Ownership safety tests for immediate Library browser renames."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from pullbox.core.exceptions import ValidationError
from pullbox.models.library import (
    FileFormat,
    LibraryFile,
    LibraryFileStorageMode,
    LibraryRoot,
)
from pullbox.services.library_rename_service import rename_library_entry

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["file", "folder"])
async def test_rename_rejects_target_containing_referenced_file(
    db_session: AsyncSession,
    tmp_path: Path,
    kind: str,
) -> None:
    root_path = tmp_path / "library"
    folder = root_path / "Existing"
    folder.mkdir(parents=True)
    source_file = folder / "Issue 001.cbz"
    original = b"user-owned comic"
    source_file.write_bytes(original)
    root = LibraryRoot(name="Comics", path=str(root_path), enabled=True)
    db_session.add(root)
    await db_session.flush()
    db_session.add(
        LibraryFile(
            file_path=str(source_file),
            file_name=source_file.name,
            file_size=len(original),
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(tz=UTC),
            library_root_id=root.id,
            storage_mode=LibraryFileStorageMode.REFERENCED,
        )
    )
    await db_session.flush()
    source = source_file if kind == "file" else folder
    target = source.with_name(f"Renamed {source.name}")

    with pytest.raises(ValidationError, match="Referenced library files cannot be renamed"):
        await rename_library_entry(
            db_session,
            source=source,
            target=target,
            kind=kind,
        )

    assert source_file.read_bytes() == original
    assert not target.exists()
