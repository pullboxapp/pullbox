"""Tests for library root resolution helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from pullbox.core.exceptions import ConfigurationError
from pullbox.core.library_root_resolution import (
    materialize_series_path,
    path_is_inside_root,
    resolve_library_root,
    resolve_path_inside_roots,
)
from pullbox.models.config import SystemConfig
from pullbox.models.library import LibraryFileStorageMode, LibraryRoot
from pullbox.models.series import Series


def _managed_root(
    *,
    name: str,
    path: Path,
    enabled: bool = True,
    allow_managed_writes: bool = True,
    is_default: bool = False,
) -> LibraryRoot:
    return LibraryRoot(
        name=name,
        path=str(path),
        enabled=enabled,
        allow_managed_writes=allow_managed_writes,
        is_default_managed_destination=is_default,
    )


@pytest.mark.asyncio
async def test_resolve_library_root_prefers_explicit_root(db_session, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()
    first = _managed_root(name="First", path=first_path)
    second = _managed_root(name="Second", path=second_path, is_default=True)
    db_session.add_all([first, second])
    await db_session.flush()

    root = await resolve_library_root(
        db_session,
        second_path / "download.cbz",
        first.id,
    )

    assert root.id == first.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("root_kwargs", "message"),
    [
        ({"enabled": False}, "disabled"),
        ({"allow_managed_writes": False}, "managed writes"),
    ],
)
async def test_resolve_library_root_rejects_unavailable_explicit_root(
    db_session,
    tmp_path: Path,
    root_kwargs: dict[str, bool],
    message: str,
) -> None:  # type: ignore[no-untyped-def]
    root_path = tmp_path / "explicit"
    root_path.mkdir()
    root = _managed_root(name="Explicit", path=root_path, **root_kwargs)
    db_session.add(root)
    await db_session.flush()

    with pytest.raises(ConfigurationError, match=message):
        await resolve_library_root(
            db_session,
            tmp_path / "imports" / "issue.cbz",
            root.id,
        )


@pytest.mark.asyncio
async def test_resolve_library_root_rejects_missing_explicit_root(db_session) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ConfigurationError, match="does not exist"):
        await resolve_library_root(db_session, Path("/imports/issue.cbz"), 999_999)


@pytest.mark.asyncio
async def test_resolve_library_root_uses_series_owned_root(
    db_session,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    root_path = tmp_path / "comics"
    series_path = root_path / "Batman (2026)"
    series_path.mkdir(parents=True)
    root = _managed_root(name="Comics", path=root_path)
    default_path = tmp_path / "default"
    default_path.mkdir()
    default = _managed_root(name="Default", path=default_path, is_default=True)
    series = Series(
        title="Batman",
        sort_title="batman",
        path=str(series_path),
        library_root_id=None,
    )
    db_session.add_all([root, default, series])
    await db_session.flush()
    series.library_root_id = root.id

    resolved = await resolve_library_root(
        db_session,
        tmp_path / "imports" / "Batman 001.cbz",
        None,
        series=series,
    )

    assert resolved.id == root.id


@pytest.mark.asyncio
async def test_resolve_library_root_prefers_series_future_destination_over_reference_root(
    db_session,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    referenced_path = tmp_path / "existing-library"
    preferred_path = tmp_path / "future-library"
    default_path = tmp_path / "default-library"
    referenced_path.mkdir()
    preferred_path.mkdir()
    default_path.mkdir()
    referenced = _managed_root(
        name="Existing",
        path=referenced_path,
        allow_managed_writes=False,
    )
    preferred = _managed_root(name="Future", path=preferred_path)
    default = _managed_root(name="Default", path=default_path, is_default=True)
    db_session.add_all([referenced, preferred, default])
    await db_session.flush()
    series = Series(
        title="Batman",
        sort_title="batman",
        path=str(referenced_path / "Batman"),
        library_root_id=referenced.id,
        preferred_library_root_id=preferred.id,
    )
    db_session.add(series)
    await db_session.flush()

    resolved = await resolve_library_root(
        db_session,
        tmp_path / "downloads" / "Batman 001.cbz",
        None,
        series=series,
    )

    assert resolved.id == preferred.id


@pytest.mark.asyncio
async def test_resolve_library_root_rejects_disabled_series_future_destination(
    db_session,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    preferred_path = tmp_path / "future-library"
    default_path = tmp_path / "default-library"
    preferred_path.mkdir()
    default_path.mkdir()
    preferred = _managed_root(name="Future", path=preferred_path, enabled=False)
    default = _managed_root(name="Default", path=default_path, is_default=True)
    db_session.add_all([preferred, default])
    await db_session.flush()
    series = Series(
        title="Batman",
        sort_title="batman",
        preferred_library_root_id=preferred.id,
    )
    db_session.add(series)
    await db_session.flush()

    with pytest.raises(ConfigurationError, match="preferred library root is disabled"):
        await resolve_library_root(
            db_session,
            tmp_path / "downloads" / "Batman 001.cbz",
            None,
            series=series,
        )


@pytest.mark.asyncio
async def test_resolve_library_root_falls_back_to_default_for_reference_only_series(
    db_session,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    referenced_path = tmp_path / "existing-library"
    default_path = tmp_path / "default-library"
    referenced_path.mkdir()
    default_path.mkdir()
    referenced = _managed_root(
        name="Existing",
        path=referenced_path,
        allow_managed_writes=False,
    )
    default = _managed_root(name="Default", path=default_path, is_default=True)
    db_session.add_all([referenced, default])
    await db_session.flush()
    series = Series(
        title="Batman",
        sort_title="batman",
        path=str(referenced_path / "Batman"),
        library_root_id=referenced.id,
    )
    db_session.add(series)
    await db_session.flush()

    resolved = await resolve_library_root(
        db_session,
        tmp_path / "downloads" / "Batman 001.cbz",
        None,
        series=series,
    )

    assert resolved.id == default.id


@pytest.mark.asyncio
async def test_resolve_library_root_uses_legacy_series_path_containment(
    db_session,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    root_path = tmp_path / "comics"
    series_path = root_path / "Batman (2026)"
    default_path = tmp_path / "default"
    series_path.mkdir(parents=True)
    default_path.mkdir()
    root = _managed_root(name="Comics", path=root_path)
    default = _managed_root(name="Default", path=default_path, is_default=True)
    series = Series(title="Batman", sort_title="batman", path=str(series_path))
    db_session.add_all([root, default, series])
    await db_session.flush()

    resolved = await resolve_library_root(
        db_session,
        tmp_path / "imports" / "Batman 001.cbz",
        None,
        series=series,
    )

    assert resolved.id == root.id


@pytest.mark.asyncio
async def test_resolve_library_root_does_not_treat_prefix_sibling_as_series_root(
    db_session,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    root_path = tmp_path / "library"
    sibling_series_path = tmp_path / "library-other" / "Batman"
    root_path.mkdir()
    sibling_series_path.mkdir(parents=True)
    root = _managed_root(name="Library", path=root_path, is_default=True)
    series = Series(title="Batman", sort_title="batman", path=str(sibling_series_path))
    db_session.add_all([root, series])
    await db_session.flush()

    with pytest.raises(ConfigurationError, match="does not belong"):
        await resolve_library_root(
            db_session,
            tmp_path / "imports" / "Batman 001.cbz",
            None,
            series=series,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "root_kwargs",
    [
        {"enabled": False},
        {"allow_managed_writes": False},
    ],
)
async def test_resolve_library_root_falls_back_from_unavailable_current_series_root(
    db_session,
    tmp_path: Path,
    root_kwargs: dict[str, bool],
) -> None:  # type: ignore[no-untyped-def]
    owned_path = tmp_path / "owned"
    default_path = tmp_path / "default"
    owned_path.mkdir()
    default_path.mkdir()
    owned = _managed_root(name="Owned", path=owned_path, **root_kwargs)
    default = _managed_root(name="Default", path=default_path, is_default=True)
    db_session.add_all([owned, default])
    await db_session.flush()
    series = Series(
        title="Batman",
        sort_title="batman",
        path=str(owned_path / "Batman"),
        library_root_id=owned.id,
    )
    db_session.add(series)
    await db_session.flush()

    resolved = await resolve_library_root(
        db_session,
        tmp_path / "imports" / "Batman 001.cbz",
        None,
        series=series,
    )

    assert resolved.id == default.id


@pytest.mark.asyncio
async def test_resolve_library_root_does_not_redirect_missing_series_root(
    db_session,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    default_path = tmp_path / "default"
    default_path.mkdir()
    db_session.add(_managed_root(name="Default", path=default_path, is_default=True))
    await db_session.flush()
    series = Series(
        title="Batman",
        sort_title="batman",
        library_root_id=999_999,
    )

    with pytest.raises(ConfigurationError, match="does not exist"):
        await resolve_library_root(
            db_session,
            tmp_path / "imports" / "Batman 001.cbz",
            None,
            series=series,
        )


@pytest.mark.asyncio
async def test_resolve_library_root_uses_default_instead_of_source_root(
    db_session,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    source_root_path = tmp_path / "source-library"
    default_path = tmp_path / "managed-default"
    source_root_path.mkdir()
    default_path.mkdir()
    source_root = _managed_root(name="Source", path=source_root_path)
    default = _managed_root(name="Default", path=default_path, is_default=True)
    db_session.add_all([source_root, default])
    await db_session.flush()

    resolved = await resolve_library_root(
        db_session,
        source_root_path / "Batman" / "Batman 001.cbz",
        None,
    )

    assert resolved.id == default.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("root_kwargs", "message"),
    [
        ({"enabled": False}, "disabled"),
        ({"allow_managed_writes": False}, "managed writes"),
    ],
)
async def test_resolve_library_root_does_not_bypass_unavailable_default_with_legacy_config(
    db_session,
    tmp_path: Path,
    root_kwargs: dict[str, bool],
    message: str,
) -> None:  # type: ignore[no-untyped-def]
    default_path = tmp_path / "default"
    legacy_path = tmp_path / "legacy"
    default_path.mkdir()
    legacy_path.mkdir()
    default = _managed_root(
        name="Default",
        path=default_path,
        is_default=True,
        **root_kwargs,
    )
    legacy = _managed_root(name="Legacy", path=legacy_path)
    db_session.add_all([default, legacy])
    db_session.add(
        SystemConfig(
            key="comics_directory",
            value=str(legacy_path),
            value_type="string",
        )
    )
    await db_session.flush()

    with pytest.raises(ConfigurationError, match=message):
        await resolve_library_root(db_session, Path("/imports/issue.cbz"), None)


@pytest.mark.asyncio
async def test_resolve_library_root_falls_back_to_comics_directory_config(
    db_session,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    root_path = tmp_path / "configured"
    root_path.mkdir()
    other_path = tmp_path / "other"
    other_path.mkdir()
    other = _managed_root(name="Other", path=other_path)
    root = _managed_root(name="Configured", path=root_path)
    db_session.add_all([other, root])
    db_session.add(
        SystemConfig(
            key="comics_directory",
            value=str(root_path / ".." / root_path.name),
            value_type="string",
        )
    )
    await db_session.flush()

    resolved = await resolve_library_root(
        db_session,
        tmp_path / "outside" / "file.cbz",
        None,
    )

    assert resolved.id == root.id


@pytest.mark.asyncio
async def test_resolve_library_root_does_not_choose_arbitrary_first_root(
    db_session,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    root_path = tmp_path / "only-root"
    root_path.mkdir()
    db_session.add(_managed_root(name="Only", path=root_path))
    await db_session.flush()

    with pytest.raises(ConfigurationError, match="No managed library destination"):
        await resolve_library_root(db_session, Path("/imports/file.cbz"), None)


@pytest.mark.asyncio
async def test_resolve_library_root_raises_without_any_configured_root(db_session) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ConfigurationError, match="No managed library destination"):
        await resolve_library_root(db_session, Path("/imports/file.cbz"), None)


def test_path_is_inside_root_and_materialize_series_path(tmp_path: Path) -> None:
    root = LibraryRoot(
        name="Comics",
        path=str(tmp_path),
        enabled=True,
        allow_managed_writes=True,
        id=10,
    )
    series_path = tmp_path / "Series"
    series = Series(title="Series", sort_title="series")

    assert path_is_inside_root(series_path / "issue.cbz", root) is True
    assert path_is_inside_root(tmp_path.parent / "other" / "issue.cbz", root) is False

    materialize_series_path(
        series,
        series_path,
        root,
        storage_mode=LibraryFileStorageMode.MANAGED,
    )

    assert series.path == str(series_path)
    assert series.library_root_id == root.id
    assert series.preferred_library_root_id == root.id


def test_materialize_series_path_preserves_explicit_future_destination(tmp_path: Path) -> None:
    current = LibraryRoot(name="Current", path=str(tmp_path / "current"), enabled=True, id=10)
    preferred = LibraryRoot(
        name="Preferred",
        path=str(tmp_path / "preferred"),
        enabled=True,
        id=20,
    )
    series = Series(
        title="Series",
        sort_title="series",
        preferred_library_root_id=preferred.id,
    )

    materialize_series_path(
        series,
        tmp_path / "current" / "Series",
        current,
        storage_mode=LibraryFileStorageMode.MANAGED,
    )

    assert series.library_root_id == current.id
    assert series.preferred_library_root_id == preferred.id


def test_materialize_series_path_does_not_choose_dual_role_root_for_referenced_file(
    tmp_path: Path,
) -> None:
    root = LibraryRoot(
        name="Dual role",
        path=str(tmp_path),
        enabled=True,
        allow_managed_writes=True,
        id=10,
    )
    series = Series(title="Series", sort_title="series")

    materialize_series_path(
        series,
        tmp_path / "Series",
        root,
        storage_mode=LibraryFileStorageMode.REFERENCED,
    )

    assert series.library_root_id == root.id
    assert series.preferred_library_root_id is None


def test_resolve_path_inside_roots_returns_resolved_child(tmp_path: Path) -> None:
    root = tmp_path / "library"
    nested = root / "Series"
    nested.mkdir(parents=True)

    resolved = resolve_path_inside_roots(nested / ".." / "Series", [root], require_dir=True)

    assert resolved == nested.resolve()


def test_resolve_path_inside_roots_rejects_prefix_sibling(tmp_path: Path) -> None:
    root = tmp_path / "library"
    sibling = tmp_path / "library-other"
    root.mkdir()
    sibling.mkdir()

    with pytest.raises(ValueError, match="outside"):
        resolve_path_inside_roots(sibling, [root], require_dir=True)


def test_resolve_path_inside_roots_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "library"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    escape = root / "escape"
    escape.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="outside"):
        resolve_path_inside_roots(escape, [root], require_dir=True)


def test_resolve_path_inside_roots_enforces_file_requirement(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()

    with pytest.raises(ValueError, match="file"):
        resolve_path_inside_roots(root, [root], require_file=True)
