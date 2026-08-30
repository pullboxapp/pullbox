"""Referenced library ownership and containment tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pullbox.core.exceptions import ConfigurationError
from pullbox.core.library_file_ownership import resolve_referenced_library_root
from pullbox.models.library import LibraryRoot

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


async def _add_root(
    session: AsyncSession,
    path: Path,
    *,
    name: str,
    enabled: bool = True,
) -> LibraryRoot:
    path.mkdir(parents=True, exist_ok=True)
    root = LibraryRoot(name=name, path=str(path), enabled=enabled)
    session.add(root)
    await session.flush()
    return root


async def test_referenced_root_rejects_sibling_prefix(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    await _add_root(db_session, tmp_path / "comics", name="Comics")
    sibling_file = tmp_path / "comics-old" / "Issue 001.cbz"
    sibling_file.parent.mkdir()
    sibling_file.write_bytes(b"comic")

    with pytest.raises(ConfigurationError, match="inside an enabled library root"):
        await resolve_referenced_library_root(db_session, sibling_file, None)


async def test_referenced_root_rejects_symlink_escape(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    library_path = tmp_path / "comics"
    await _add_root(db_session, library_path, name="Comics")
    outside_path = tmp_path / "outside"
    outside_path.mkdir()
    outside_file = outside_path / "Issue 001.cbz"
    outside_file.write_bytes(b"comic")
    (library_path / "escape").symlink_to(outside_path, target_is_directory=True)

    with pytest.raises(ConfigurationError, match="inside an enabled library root"):
        await resolve_referenced_library_root(
            db_session,
            library_path / "escape" / outside_file.name,
            None,
        )


async def test_referenced_root_selects_deepest_nested_root(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    outer = await _add_root(db_session, tmp_path / "comics", name="All Comics")
    inner = await _add_root(db_session, tmp_path / "comics" / "dc", name="DC")
    source = tmp_path / "comics" / "dc" / "Batman" / "Issue 001.cbz"
    source.parent.mkdir()
    source.write_bytes(b"comic")

    selected_root, resolved_source, signature = await resolve_referenced_library_root(
        db_session,
        source,
        None,
    )

    assert selected_root.id == inner.id
    assert selected_root.id != outer.id
    assert resolved_source == source.resolve()
    assert signature["resolved_path"] == str(source.resolve())


async def test_referenced_root_rejects_disabled_or_wrong_explicit_root(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    enabled = await _add_root(db_session, tmp_path / "enabled", name="Enabled")
    disabled = await _add_root(
        db_session,
        tmp_path / "disabled",
        name="Disabled",
        enabled=False,
    )
    source = tmp_path / "enabled" / "Issue 001.cbz"
    source.write_bytes(b"comic")

    with pytest.raises(ConfigurationError, match="missing or disabled"):
        await resolve_referenced_library_root(db_session, source, disabled.id)

    other = await _add_root(db_session, tmp_path / "other", name="Other")
    with pytest.raises(ConfigurationError, match="inside an enabled library root"):
        await resolve_referenced_library_root(db_session, source, other.id)

    selected_root, _, _ = await resolve_referenced_library_root(
        db_session,
        source,
        enabled.id,
    )
    assert selected_root.id == enabled.id


async def test_referenced_root_rejects_parent_components(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    library_path = tmp_path / "comics"
    await _add_root(db_session, library_path, name="Comics")
    source = library_path / "Issue 001.cbz"
    source.write_bytes(b"comic")

    with pytest.raises(ConfigurationError, match="unsafe path components"):
        await resolve_referenced_library_root(
            db_session,
            library_path / "unused" / ".." / source.name,
            None,
        )
